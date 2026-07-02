"""Branch-friendly campaign long-term memory."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import (
    Campaign,
    CampaignMemory,
    CampaignSnapshot,
    MemoryRevision,
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
    ) -> MemoryInfo:
        with self.database.transaction() as session:
            if session.get(Campaign, campaign_id) is None:
                raise CampaignNotFoundError(campaign_id)
            if snapshot_id and session.get(CampaignSnapshot, snapshot_id) is None:
                raise LookupError(snapshot_id)
            memory = CampaignMemory(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                kind=kind,
                subject=subject,
            )
            revision = MemoryRevision(
                id=str(uuid.uuid4()),
                memory_id=memory.id,
                snapshot_id=snapshot_id,
                content=content,
                metadata_json=metadata or {},
            )
            session.add_all([memory, revision])
            session.flush()
            return self._info(memory, revision)

    def revise(
        self,
        memory_id: str,
        *,
        content: str,
        metadata: dict[str, Any] | None = None,
        snapshot_id: str | None = None,
    ) -> MemoryInfo:
        with self.database.transaction() as session:
            memory = session.get(CampaignMemory, memory_id)
            if memory is None:
                raise LookupError(memory_id)
            current = session.scalar(
                select(MemoryRevision)
                .where(
                    MemoryRevision.memory_id == memory_id,
                    MemoryRevision.active.is_(True),
                )
                .order_by(MemoryRevision.created_at.desc())
            )
            if current:
                current.active = False
            revision = MemoryRevision(
                id=str(uuid.uuid4()),
                memory_id=memory_id,
                parent_id=current.id if current else None,
                snapshot_id=snapshot_id,
                content=content,
                metadata_json=metadata or {},
            )
            session.add(revision)
            session.flush()
            return self._info(memory, revision)

    def list(self, campaign_id: str, *, kind: str | None = None) -> list[MemoryInfo]:
        with self.database.transaction() as session:
            statement = (
                select(CampaignMemory, MemoryRevision)
                .join(MemoryRevision, MemoryRevision.memory_id == CampaignMemory.id)
                .where(
                    CampaignMemory.campaign_id == campaign_id,
                    MemoryRevision.active.is_(True),
                )
                .order_by(CampaignMemory.updated_at.desc(), CampaignMemory.id)
            )
            if kind:
                statement = statement.where(CampaignMemory.kind == kind)
            return [self._info(*row) for row in session.execute(statement)]

    def search(
        self,
        campaign_id: str,
        query: str,
        *,
        limit: int = 8,
    ) -> list[MemoryInfo]:
        values = self.list(campaign_id)
        ranked = sorted(
            values,
            key=lambda item: -lexical_score(
                query,
                title=item.subject,
                content=item.content,
            ),
        )
        return ranked[: max(1, min(limit, 100))]

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
        )
