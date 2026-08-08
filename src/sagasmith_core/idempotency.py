"""Idempotency records for safe MCP retries."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
import zlib
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select

from sagasmith_core.database import Database
from sagasmith_core.integrity import canonical_json, json_sha256
from sagasmith_core.models import Campaign, IdempotencyRecord, MutationGroup, StateRevision


class IdempotencyConflictError(ValueError):
    pass


_COMPRESSED_RESPONSE_MARKER = "sagasmith.idempotency.response+zlib.v1"
_COMPRESSED_RESPONSE_THRESHOLD = 64 * 1024
_MAX_UNCOMPRESSED_RESPONSE_BYTES = 512 * 1024 * 1024


def _stored_response(response: dict[str, Any]) -> dict[str, Any]:
    """Compress large exact replay values without changing their public shape."""

    raw = canonical_json(response).encode("utf-8")
    marker_collision = "_sagasmith_encoding" in response
    if len(raw) < _COMPRESSED_RESPONSE_THRESHOLD and not marker_collision:
        return dict(response)
    compressed = zlib.compress(raw)
    envelope = {
        "_sagasmith_encoding": _COMPRESSED_RESPONSE_MARKER,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "uncompressed_bytes": len(raw),
        "data_base64": base64.b64encode(compressed).decode("ascii"),
    }
    if not marker_collision and len(canonical_json(envelope).encode("utf-8")) >= len(raw):
        return dict(response)
    return envelope


def _public_response(stored: dict[str, Any]) -> dict[str, Any]:
    """Decode a response stored either directly or in the compressed envelope."""

    if stored.get("_sagasmith_encoding") != _COMPRESSED_RESPONSE_MARKER:
        return dict(stored)
    expected_keys = {
        "_sagasmith_encoding",
        "sha256",
        "uncompressed_bytes",
        "data_base64",
    }
    if set(stored) != expected_keys:
        raise RuntimeError("compressed idempotency response envelope has unexpected fields")
    expected_size = stored.get("uncompressed_bytes")
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
        or expected_size > _MAX_UNCOMPRESSED_RESPONSE_BYTES
    ):
        raise RuntimeError("compressed idempotency response size is invalid")
    encoded = stored.get("data_base64")
    if not isinstance(encoded, str):
        raise RuntimeError("compressed idempotency response payload is invalid")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, expected_size + 1)
    except (ValueError, zlib.error) as exc:
        raise RuntimeError("compressed idempotency response cannot be decoded") from exc
    # ``flush()`` after a bounded ``decompress()`` can expand the remaining
    # stream without a limit. A valid single zlib member whose declared size is
    # correct reaches EOF in the bounded call; anything else is corrupt.
    if (
        inflater.unconsumed_tail
        or not inflater.eof
        or inflater.unused_data
        or len(raw) != expected_size
    ):
        raise RuntimeError("compressed idempotency response length does not match its receipt")
    if hashlib.sha256(raw).hexdigest() != stored.get("sha256"):
        raise RuntimeError("compressed idempotency response checksum does not match its receipt")
    try:
        response = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("compressed idempotency response is not valid JSON") from exc
    if not isinstance(response, dict):
        raise RuntimeError("compressed idempotency response must decode to an object")
    return response


@dataclass(frozen=True)
class IdempotencyWrite:
    """Persist an exact public replay response with its owning transaction."""

    scope: str
    payload: Any
    response: dict[str, Any] | Callable[[Any], dict[str, Any]]


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
    return json_sha256(payload)


class IdempotencyService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def lookup(self, scope: str, key: str, payload: Any) -> IdempotencyResult | None:
        with self.database.transaction() as session:
            return self.lookup_in_session(session, scope, key, payload)

    def receipt(
        self,
        campaign_id: str | None,
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
                groups = list(
                    session.scalars(
                        select(MutationGroup).where(
                            MutationGroup.campaign_id == campaign_id,
                            MutationGroup.branch_id == effective_branch_id,
                            MutationGroup.idempotency_key == key,
                            MutationGroup.applied.is_(True),
                        )
                    )
                )
                if not groups:
                    raise LookupError(f"idempotency receipt not found: {key}")
                if len(groups) != 1:
                    raise RuntimeError(f"idempotency mutation group is ambiguous: {key}")
                group = groups[0]
                entity_revisions = []
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
                    {
                        "status": "committed",
                        "idempotency_replayed": True,
                        "response_recovery": "read_current_state",
                    },
                    group.id,
                    str(group.request_hash or ""),
                    group.branch_id,
                    entity_revisions,
                )
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
                _public_response(dict(row.response)),
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
        return IdempotencyResult(
            key,
            True,
            _public_response(dict(row.response)),
            row.mutation_group_id,
        )

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
            return IdempotencyResult(
                key,
                True,
                _public_response(dict(row.response)),
                row.mutation_group_id,
            )
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
            response=_stored_response(response),
        )
        session.add(row)
        session.flush()
        return IdempotencyResult(key, False, dict(response), row.mutation_group_id)

    def require_uncommitted_in_session(
        self,
        session,
        key: str | None,
        write: IdempotencyWrite | None,
    ) -> None:
        """Reject a duplicate before a non-state domain mutation is applied."""

        if (key is None) != (write is None):
            raise ValueError("idempotency_key and idempotency_write must be supplied together")
        if write is None:
            return
        if not str(write.scope).strip():
            raise ValueError("idempotency_write.scope is required")
        replay = self.lookup_in_session(session, write.scope, str(key), write.payload)
        if replay is not None:
            raise ValueError(
                "idempotency key already has a committed response; "
                "read its replay receipt instead of applying another write"
            )

    def remember_write_in_session(
        self,
        session,
        *,
        campaign_id: str,
        key: str | None,
        write: IdempotencyWrite | None,
        result: Any,
        mutation_group_id: str | None = None,
    ) -> None:
        """Save a domain result's exact replay response before the transaction commits."""

        if write is None:
            return
        response = write.response(result) if callable(write.response) else write.response
        if not isinstance(response, dict):
            raise ValueError("idempotency response builder must return an object")
        self.remember_in_session(
            session,
            write.scope,
            str(key),
            write.payload,
            response,
            campaign_id=campaign_id,
            mutation_group_id=mutation_group_id,
        )
