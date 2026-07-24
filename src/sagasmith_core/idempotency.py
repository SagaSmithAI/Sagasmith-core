"""Idempotency records for safe MCP retries."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from sagasmith_core.database import Database
from sagasmith_core.models import Campaign, IdempotencyRecord, MutationGroup, StateRevision


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

    def receipt(
        self,
        campaign_id: str,
        key: str,
        *,
        branch_id: str | None = None,
    ) -> IdempotencyReceipt:
        """Read one campaign-owned replay receipt without reconstructing its request."""
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise LookupError(campaign_id)
            effective_branch_id = branch_id or campaign.active_branch_id
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
            matched: list[tuple[IdempotencyRecord, MutationGroup | None]] = []
            for candidate in rows:
                candidate_group = (
                    session.get(MutationGroup, candidate.mutation_group_id)
                    if candidate.mutation_group_id
                    else None
                )
                if candidate_group is None:
                    candidate_group = session.scalar(
                        select(MutationGroup).where(
                            MutationGroup.campaign_id == campaign_id,
                            MutationGroup.branch_id == effective_branch_id,
                            MutationGroup.idempotency_key == key,
                        )
                    )
                if candidate_group is not None:
                    if candidate_group.branch_id == effective_branch_id:
                        matched.append((candidate, candidate_group))
                elif len(rows) == 1:
                    matched.append((candidate, None))
            if not matched:
                raise LookupError(
                    f"idempotency receipt not found on branch {effective_branch_id}: {key}"
                )
            if len(matched) != 1:
                raise RuntimeError(
                    f"idempotency receipt is ambiguous on branch {effective_branch_id}: {key}"
                )
            row, group = matched[0]
            if group is None:
                groups = list(
                    session.scalars(
                        select(MutationGroup).where(
                            MutationGroup.campaign_id == campaign_id,
                            MutationGroup.branch_id == effective_branch_id,
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

    def mutation_committed(
        self,
        campaign_id: str,
        key: str,
        payload: Any | None = None,
        *,
        branch_id: str | None = None,
    ) -> bool:
        """Check for a state commit whose richer replay receipt is absent."""
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                return False
            effective_branch_id = branch_id or campaign.active_branch_id
            row = session.scalar(
                select(MutationGroup).where(
                    MutationGroup.campaign_id == campaign_id,
                    MutationGroup.branch_id == effective_branch_id,
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
        if mutation_group_id is None and campaign_id is not None:
            groups = list(
                session.scalars(
                    select(MutationGroup).where(
                        MutationGroup.campaign_id == campaign_id,
                        MutationGroup.idempotency_key == key,
                        MutationGroup.applied.is_(True),
                    )
                )
            )
            scope_parts = set(scope.split(":"))
            scoped_groups = [
                group
                for group in groups
                if group.branch_id is not None and group.branch_id in scope_parts
            ]
            if len(scoped_groups) == 1:
                mutation_group_id = scoped_groups[0].id
            elif len(groups) == 1:
                mutation_group_id = groups[0].id
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
