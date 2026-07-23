"""Branch-friendly campaign long-term memory."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from sagasmith_core.branches import resolve_branch
from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import (
    BranchFactHead,
    Campaign,
    CampaignMemory,
    CampaignSnapshot,
    MemoryRevision,
    utcnow,
)
from sagasmith_core.retrieval import lexical_score


@dataclass(frozen=True)
class MemoryInfo:
    id: str
    campaign_id: str
    kind: str
    subject: str
    revision_id: str
    content: str
    metadata: dict[str, Any]
    snapshot_id: str | None
    fact_key: str
    subject_ref: str
    predicate: str
    status: str
    valid_from: str | None
    valid_to: str | None
    source_event_ids: list[str]
    importance: int
    disclosure_scope: str
    created_at: str
    updated_at: str


_STATUSES = {"active", "superseded", "retracted"}
_DISCLOSURE_SCOPES = {"dm", "public", "party", "player"}


class MemoryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self,
        campaign_id: str,
        *,
        content: str,
        kind: str = "fact",
        subject: str = "",
        metadata: dict[str, Any] | None = None,
        snapshot_id: str | None = None,
        branch_id: str | None = None,
        fact_key: str | None = None,
        subject_ref: str = "",
        predicate: str = "",
        status: str = "active",
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        source_event_ids: list[str] | None = None,
        importance: int = 3,
        disclosure_scope: str | None = None,
    ) -> MemoryInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            if fact_key is not None:
                normalized_key = self._validate_fact_key(fact_key)
                existing = session.scalar(
                    select(CampaignMemory).where(
                        CampaignMemory.campaign_id == campaign_id,
                        CampaignMemory.fact_key == normalized_key,
                    )
                )
                if existing is not None:
                    head = session.get(
                        BranchFactHead,
                        {"branch_id": branch.id, "memory_id": existing.id},
                    )
                    if head is not None:
                        raise ValueError(f"campaign fact already exists: {normalized_key}")
                    return self._add_branch_revision_in_session(
                        session,
                        existing,
                        branch.id,
                        content=content,
                        metadata=metadata,
                        snapshot_id=snapshot_id,
                        status=status,
                        valid_from=valid_from,
                        valid_to=valid_to,
                        source_event_ids=source_event_ids,
                        importance=importance,
                        disclosure_scope=disclosure_scope,
                    )
            return self._add_in_session(
                session,
                campaign_id,
                branch.id,
                content=content,
                kind=kind,
                subject=subject,
                metadata=metadata,
                snapshot_id=snapshot_id,
                fact_key=fact_key,
                subject_ref=subject_ref,
                predicate=predicate,
                status=status,
                valid_from=valid_from,
                valid_to=valid_to,
                source_event_ids=source_event_ids,
                importance=importance,
                disclosure_scope=disclosure_scope,
            )

    def revise(
        self,
        memory_id: str,
        *,
        content: str,
        metadata: dict[str, Any] | None = None,
        snapshot_id: str | None = None,
        branch_id: str | None = None,
        expected_revision_id: str | None = None,
        status: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        source_event_ids: list[str] | None = None,
        importance: int | None = None,
        disclosure_scope: str | None = None,
    ) -> MemoryInfo:
        with self.database.transaction() as session:
            memory = session.get(CampaignMemory, memory_id)
            if memory is None:
                raise LookupError(memory_id)
            campaign = session.get(Campaign, memory.campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(memory.campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            return self._revise_in_session(
                session,
                memory,
                branch.id,
                content=content,
                metadata=metadata,
                snapshot_id=snapshot_id,
                expected_revision_id=expected_revision_id,
                status=status,
                valid_from=valid_from,
                valid_to=valid_to,
                source_event_ids=source_event_ids,
                importance=importance,
                disclosure_scope=disclosure_scope,
            )

    def upsert(
        self,
        campaign_id: str,
        *,
        fact_key: str,
        content: str,
        kind: str = "fact",
        subject: str = "",
        subject_ref: str = "",
        predicate: str = "",
        metadata: dict[str, Any] | None = None,
        snapshot_id: str | None = None,
        branch_id: str | None = None,
        expected_revision_id: str | None = None,
        status: str = "active",
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        source_event_ids: list[str] | None = None,
        importance: int = 3,
        disclosure_scope: str | None = None,
    ) -> MemoryInfo:
        normalized_key = self._validate_fact_key(fact_key)
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            memory = session.scalar(
                select(CampaignMemory).where(
                    CampaignMemory.campaign_id == campaign_id,
                    CampaignMemory.fact_key == normalized_key,
                )
            )
            if memory is None:
                if expected_revision_id is not None:
                    raise ValueError("expected revision cannot target a missing fact")
                return self._add_in_session(
                    session,
                    campaign_id,
                    branch.id,
                    content=content,
                    kind=kind,
                    subject=subject,
                    metadata=metadata,
                    snapshot_id=snapshot_id,
                    fact_key=normalized_key,
                    subject_ref=subject_ref,
                    predicate=predicate,
                    status=status,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    source_event_ids=source_event_ids,
                    importance=importance,
                    disclosure_scope=disclosure_scope,
                )
            head = session.get(
                BranchFactHead,
                {"branch_id": branch.id, "memory_id": memory.id},
            )
            if head is None:
                if expected_revision_id is not None:
                    raise ValueError("expected revision cannot target a missing branch fact")
                return self._add_branch_revision_in_session(
                    session,
                    memory,
                    branch.id,
                    content=content,
                    metadata=metadata,
                    snapshot_id=snapshot_id,
                    status=status,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    source_event_ids=source_event_ids,
                    importance=importance,
                    disclosure_scope=disclosure_scope,
                )
            return self._revise_in_session(
                session,
                memory,
                branch.id,
                content=content,
                metadata=metadata,
                snapshot_id=snapshot_id,
                expected_revision_id=expected_revision_id,
                status=status,
                valid_from=valid_from,
                valid_to=valid_to,
                source_event_ids=source_event_ids,
                importance=importance,
                disclosure_scope=disclosure_scope,
            )

    def list(
        self,
        campaign_id: str,
        *,
        kind: str | None = None,
        branch_id: str | None = None,
        include_inactive: bool = False,
    ) -> list[MemoryInfo]:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            statement = (
                select(CampaignMemory, MemoryRevision)
                .join(BranchFactHead, BranchFactHead.memory_id == CampaignMemory.id)
                .join(MemoryRevision, MemoryRevision.id == BranchFactHead.revision_id)
                .where(BranchFactHead.branch_id == branch.id)
                .order_by(CampaignMemory.updated_at.desc(), CampaignMemory.id)
            )
            if kind:
                statement = statement.where(CampaignMemory.kind == kind)
            if not include_inactive:
                statement = statement.where(MemoryRevision.status == "active")
            return [self._info(*row) for row in session.execute(statement)]

    def search(
        self,
        campaign_id: str,
        query: str,
        *,
        limit: int = 8,
        branch_id: str | None = None,
        include_inactive: bool = False,
    ) -> list[MemoryInfo]:
        values = self.list(
            campaign_id, branch_id=branch_id, include_inactive=include_inactive
        )
        ranked = sorted(
            values,
            key=lambda item: (
                -lexical_score(
                    query,
                    title=" ".join(
                        value
                        for value in (
                            item.subject,
                            item.subject_ref,
                            item.predicate,
                            item.fact_key,
                        )
                        if value
                    ),
                    content=item.content,
                ),
                -item.importance,
                item.fact_key,
            ),
        )
        return ranked[: max(1, min(limit, 100))]

    def _add_in_session(
        self,
        session,
        campaign_id: str,
        branch_id: str,
        *,
        content: str,
        kind: str,
        subject: str,
        metadata: dict[str, Any] | None,
        snapshot_id: str | None,
        fact_key: str | None,
        subject_ref: str,
        predicate: str,
        status: str,
        valid_from: datetime | None,
        valid_to: datetime | None,
        source_event_ids: list[str] | None,
        importance: int,
        disclosure_scope: str | None,
    ) -> MemoryInfo:
        memory_id = str(uuid.uuid4())
        memory = CampaignMemory(
            id=memory_id,
            campaign_id=campaign_id,
            kind=kind,
            subject=subject,
            fact_key=self._validate_fact_key(fact_key or f"legacy:{memory_id}"),
            subject_ref=subject_ref,
            predicate=predicate,
        )
        session.add(memory)
        session.flush()
        return self._add_branch_revision_in_session(
            session,
            memory,
            branch_id,
            content=content,
            metadata=metadata,
            snapshot_id=snapshot_id,
            status=status,
            valid_from=valid_from,
            valid_to=valid_to,
            source_event_ids=source_event_ids,
            importance=importance,
            disclosure_scope=disclosure_scope,
        )

    def _add_branch_revision_in_session(
        self,
        session,
        memory: CampaignMemory,
        branch_id: str,
        *,
        content: str,
        metadata: dict[str, Any] | None,
        snapshot_id: str | None,
        status: str,
        valid_from: datetime | None,
        valid_to: datetime | None,
        source_event_ids: list[str] | None,
        importance: int,
        disclosure_scope: str | None,
    ) -> MemoryInfo:
        self._validate_snapshot(session, memory.campaign_id, snapshot_id)
        self._validate_revision_fields(status, importance, disclosure_scope, metadata)
        resolved_scope = disclosure_scope or str((metadata or {}).get("disclosure_scope", "dm"))
        revision = MemoryRevision(
            id=str(uuid.uuid4()),
            memory_id=memory.id,
            snapshot_id=snapshot_id,
            content=content,
            metadata_json=metadata or {},
            active=status == "active",
            status=status,
            valid_from=valid_from,
            valid_to=valid_to,
            source_event_ids=list(source_event_ids or []),
            importance=importance,
            disclosure_scope=resolved_scope,
        )
        session.add(revision)
        session.flush()
        session.add(
            BranchFactHead(branch_id=branch_id, memory_id=memory.id, revision_id=revision.id)
        )
        memory.updated_at = utcnow()
        return self._info(memory, revision)

    def _revise_in_session(
        self,
        session,
        memory: CampaignMemory,
        branch_id: str,
        *,
        content: str,
        metadata: dict[str, Any] | None,
        snapshot_id: str | None,
        expected_revision_id: str | None,
        status: str | None,
        valid_from: datetime | None,
        valid_to: datetime | None,
        source_event_ids: list[str] | None,
        importance: int | None,
        disclosure_scope: str | None,
    ) -> MemoryInfo:
        self._validate_snapshot(session, memory.campaign_id, snapshot_id)
        head = session.get(BranchFactHead, {"branch_id": branch_id, "memory_id": memory.id})
        if head is None:
            raise LookupError(f"memory {memory.id} is not visible on branch {branch_id}")
        current = session.get(MemoryRevision, head.revision_id)
        if current is None:
            raise LookupError(head.revision_id)
        if expected_revision_id is not None and current.id != expected_revision_id:
            raise ValueError(
                f"expected memory revision {expected_revision_id}, current revision is {current.id}"
            )
        resolved_status = status or current.status
        resolved_importance = importance if importance is not None else current.importance
        resolved_metadata = dict(current.metadata_json) if metadata is None else dict(metadata)
        resolved_scope = disclosure_scope or current.disclosure_scope
        self._validate_revision_fields(
            resolved_status, resolved_importance, resolved_scope, resolved_metadata
        )
        revision = MemoryRevision(
            id=str(uuid.uuid4()),
            memory_id=memory.id,
            parent_id=current.id,
            snapshot_id=snapshot_id,
            content=content,
            metadata_json=resolved_metadata,
            active=resolved_status == "active",
            status=resolved_status,
            valid_from=valid_from if valid_from is not None else current.valid_from,
            valid_to=valid_to if valid_to is not None else current.valid_to,
            source_event_ids=(
                list(source_event_ids)
                if source_event_ids is not None
                else list(current.source_event_ids)
            ),
            importance=resolved_importance,
            disclosure_scope=resolved_scope,
        )
        session.add(revision)
        session.flush()
        head.revision_id = revision.id
        memory.updated_at = utcnow()
        return self._info(memory, revision)

    @staticmethod
    def _validate_snapshot(session, campaign_id: str, snapshot_id: str | None) -> None:
        if not snapshot_id:
            return
        snapshot = session.get(CampaignSnapshot, snapshot_id)
        if snapshot is None or snapshot.campaign_id != campaign_id:
            raise LookupError(snapshot_id)

    @staticmethod
    def _validate_fact_key(fact_key: str) -> str:
        value = fact_key.strip()
        if not value or len(value) > 300:
            raise ValueError("fact_key must contain 1-300 characters")
        return value

    @staticmethod
    def _validate_revision_fields(
        status: str,
        importance: int,
        disclosure_scope: str | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        if status not in _STATUSES:
            raise ValueError(f"invalid campaign-memory status: {status}")
        if not 1 <= importance <= 5:
            raise ValueError("campaign-memory importance must be between 1 and 5")
        scope = disclosure_scope or str((metadata or {}).get("disclosure_scope", "dm"))
        if scope not in _DISCLOSURE_SCOPES:
            raise ValueError(f"invalid campaign-memory disclosure scope: {scope}")

    @staticmethod
    def _info(memory: CampaignMemory, revision: MemoryRevision) -> MemoryInfo:
        return MemoryInfo(
            id=memory.id,
            campaign_id=memory.campaign_id,
            kind=memory.kind,
            subject=memory.subject,
            revision_id=revision.id,
            content=revision.content,
            metadata=dict(revision.metadata_json),
            snapshot_id=revision.snapshot_id,
            fact_key=memory.fact_key,
            subject_ref=memory.subject_ref,
            predicate=memory.predicate,
            status=revision.status,
            valid_from=revision.valid_from.isoformat() if revision.valid_from else None,
            valid_to=revision.valid_to.isoformat() if revision.valid_to else None,
            source_event_ids=list(revision.source_event_ids),
            importance=revision.importance,
            disclosure_scope=revision.disclosure_scope,
            created_at=memory.created_at.isoformat(),
            updated_at=memory.updated_at.isoformat(),
        )
