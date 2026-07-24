"""Idempotency records for safe MCP retries."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from sagasmith_core.database import Database
from sagasmith_core.models import IdempotencyRecord, MutationGroup, StateRevision


class IdempotencyConflictError(ValueError):
    pass


@dataclass(frozen=True)
class IdempotencyResult:
    key: str
    replayed: bool
    response: dict[str, Any] | None
    mutation_group_id: str | None


@dataclass(frozen=True)
class IdempotencyReceipt:
    key: str
    replayed: bool
    response: dict[str, Any]
    mutation_group_id: str | None
    request_hash: str
    branch_id: str | None
    entity_revisions: list[dict[str, Any]]


def request_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class IdempotencyService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def lookup(self, scope: str, key: str, payload: Any) -> IdempotencyResult | None:
        with self.database.transaction() as session:
            return self.lookup_in_session(session, scope, key, payload)

    def receipt(self, campaign_id: str, key: str) -> IdempotencyReceipt:
        """Read one campaign-owned replay receipt without reconstructing its request."""
        with self.database.transaction() as session:
            rows = list(
                session.scalars(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.campaign_id == campaign_id,
                        IdempotencyRecord.key == key,
                    )
                )
            )
            if not rows:
                raise LookupError(f"idempotency receipt not found: {key}")
            if len(rows) != 1:
                raise RuntimeError(f"idempotency receipt is ambiguous: {key}")
            row = rows[0]
            group = (
                session.get(MutationGroup, row.mutation_group_id)
                if row.mutation_group_id
                else None
            )
            if group is None:
                groups = list(
                    session.scalars(
                        select(MutationGroup).where(
                            MutationGroup.campaign_id == campaign_id,
                            MutationGroup.idempotency_key == key,
                        )
                    )
                )
                if len(groups) > 1:
                    raise RuntimeError(f"idempotency mutation group is ambiguous: {key}")
                group = groups[0] if groups else None
            entity_revisions = []
            if group is not None:
                revision_rows = session.scalars(
                    select(StateRevision)
                    .where(StateRevision.mutation_group_id == group.id)
                    .order_by(StateRevision.sequence)
                )
                for revision in revision_rows:
                    before = dict(revision.before or {})
                    after = dict(revision.after or {})
                    entity_revisions.append(
                        {
                            "entity_type": revision.entity_type,
                            "entity_id": revision.entity_id,
                            "before_revision": before.get("revision"),
                            "after_revision": after.get("revision"),
                        }
                    )
            return IdempotencyReceipt(
                key,
                True,
                dict(row.response),
                group.id if group is not None else row.mutation_group_id,
                row.request_hash,
                group.branch_id if group is not None else None,
                entity_revisions,
            )

    def lookup_in_session(
        self, session, scope: str, key: str, payload: Any
    ) -> IdempotencyResult | None:
        digest = request_hash(payload)
        row = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.key == key,
            )
        )
        if row is None:
            return None
        if row.request_hash != digest:
            raise IdempotencyConflictError(
                f"idempotency key reused with a different request: {key}"
            )
        return IdempotencyResult(key, True, dict(row.response), row.mutation_group_id)

    def mutation_committed(self, campaign_id: str, key: str, payload: Any | None = None) -> bool:
        """Check for a state commit whose richer replay receipt is absent."""
        with self.database.transaction() as session:
            row = session.scalar(
                select(MutationGroup).where(
                    MutationGroup.campaign_id == campaign_id,
                    MutationGroup.idempotency_key == key,
                    MutationGroup.applied.is_(True),
                )
            )
            if row is None:
                return False
            if (
                payload is not None
                and row.request_hash
                and row.request_hash != request_hash(payload)
            ):
                raise IdempotencyConflictError(
                    f"idempotency key reused with a different request: {key}"
                )
            return True

    def remember(
        self,
        scope: str,
        key: str,
        payload: Any,
        response: dict[str, Any],
        *,
        campaign_id: str | None = None,
        mutation_group_id: str | None = None,
    ) -> IdempotencyResult:
        with self.database.transaction() as session:
            return self.remember_in_session(
                session,
                scope,
                key,
                payload,
                response,
                campaign_id=campaign_id,
                mutation_group_id=mutation_group_id,
            )

    def remember_in_session(
        self,
        session,
        scope: str,
        key: str,
        payload: Any,
        response: dict[str, Any],
        *,
        campaign_id: str | None = None,
        mutation_group_id: str | None = None,
    ) -> IdempotencyResult:
        digest = request_hash(payload)
        row = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.key == key,
            )
        )
        if row is not None:
            if row.request_hash != digest:
                raise IdempotencyConflictError(
                    f"idempotency key reused with a different request: {key}"
                )
            return IdempotencyResult(key, True, dict(row.response), row.mutation_group_id)
        row = IdempotencyRecord(
            id=str(uuid.uuid4()),
            scope=scope,
            key=key,
            campaign_id=campaign_id,
            request_hash=digest,
            mutation_group_id=mutation_group_id,
            response=dict(response),
        )
        session.add(row)
        session.flush()
        return IdempotencyResult(key, False, dict(row.response), row.mutation_group_id)
