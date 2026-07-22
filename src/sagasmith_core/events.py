"""Campaign-scoped event log."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update

from sagasmith_core.branches import resolve_branch
from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import (
    ActorKnowledge,
    ActorKnowledgeRevision,
    BranchActorKnowledgeHead,
    Campaign,
    CampaignEvent,
    Character,
    SnapshotEventBinding,
)

_AUDIENCE_SCOPES = {"dm", "public", "party", "player", "actor"}


@dataclass(frozen=True)
class CampaignEventInfo:
    id: str
    campaign_id: str
    sequence: int
    event_type: str
    summary: str
    payload: dict[str, Any]
    audience_scope: str
    created_at: str


class EventService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self,
        campaign_id: str,
        *,
        event_type: str = "narrative",
        summary: str,
        payload: dict[str, Any] | None = None,
        audience_scope: str = "dm",
        branch_id: str | None = None,
    ) -> CampaignEventInfo:
        if audience_scope not in _AUDIENCE_SCOPES:
            raise ValueError(f"invalid event audience scope: {audience_scope}")
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            return self._add_in_session(
                session,
                campaign,
                branch.id,
                event_type=event_type,
                summary=summary,
                payload=payload,
                audience_scope=audience_scope,
            )

    def add_with_actor_knowledge(
        self,
        campaign_id: str,
        *,
        summary: str,
        actor_ids: list[str],
        knowledge_key: str,
        proposition: str,
        event_type: str = "narrative",
        payload: dict[str, Any] | None = None,
        audience_scope: str = "dm",
        disclosure_scope: str = "owner",
        branch_id: str | None = None,
    ) -> tuple[CampaignEventInfo, list[str]]:
        """Append one event and every witnessed knowledge head atomically."""

        if audience_scope not in _AUDIENCE_SCOPES:
            raise ValueError(f"invalid event audience scope: {audience_scope}")
        if disclosure_scope not in {"dm", "owner", "party", "public", "player"}:
            raise ValueError(f"invalid actor-knowledge disclosure scope: {disclosure_scope}")
        normalized_actor_ids = [str(item) for item in actor_ids]
        if not normalized_actor_ids:
            raise ValueError("actor_ids must not be empty")
        if len(set(normalized_actor_ids)) != len(normalized_actor_ids):
            raise ValueError("actor_ids must be unique")
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            actors = [session.get(Character, actor_id) for actor_id in normalized_actor_ids]
            if any(actor is None or actor.campaign_id != campaign_id for actor in actors):
                raise ValueError("every knowledge actor must be a live character in this campaign")

            knowledge_rows: list[ActorKnowledge] = []
            for actor_id in normalized_actor_ids:
                knowledge = session.scalar(
                    select(ActorKnowledge).where(
                        ActorKnowledge.actor_id == actor_id,
                        ActorKnowledge.knowledge_key == knowledge_key,
                    )
                )
                if knowledge is not None:
                    head = session.get(
                        BranchActorKnowledgeHead,
                        {"branch_id": branch.id, "knowledge_id": knowledge.id},
                    )
                    if head is not None:
                        raise ValueError(
                            f"knowledge key already exists for actor: {knowledge_key}"
                        )
                else:
                    knowledge = ActorKnowledge(
                        id=str(uuid.uuid4()),
                        campaign_id=campaign_id,
                        actor_id=actor_id,
                        knowledge_key=knowledge_key,
                        subject_ref="",
                    )
                knowledge_rows.append(knowledge)

            event_info = self._add_in_session(
                session,
                campaign,
                branch.id,
                event_type=event_type,
                summary=summary,
                payload=payload,
                audience_scope=audience_scope,
            )
            event = session.get(CampaignEvent, event_info.id)
            assert event is not None
            knowledge_ids: list[str] = []
            for knowledge in knowledge_rows:
                revision = ActorKnowledgeRevision(
                    id=str(uuid.uuid4()),
                    knowledge_id=knowledge.id,
                    proposition=proposition,
                    epistemic_status="known",
                    confidence=3,
                    source_event_id=event.id,
                    cause="witnessed",
                    disclosure_scope=disclosure_scope,
                )
                session.add_all([knowledge, revision])
                session.flush()
                session.add(
                    BranchActorKnowledgeHead(
                        branch_id=branch.id,
                        knowledge_id=knowledge.id,
                        revision_id=revision.id,
                    )
                )
                knowledge_ids.append(knowledge.id)
            session.flush()
            return event_info, knowledge_ids

    def _add_in_session(
        self,
        session,
        campaign: Campaign,
        branch_id: str,
        *,
        event_type: str,
        summary: str,
        payload: dict[str, Any] | None,
        audience_scope: str,
    ) -> CampaignEventInfo:
        if audience_scope not in _AUDIENCE_SCOPES:
            raise ValueError(f"invalid event audience scope: {audience_scope}")
        sequence = session.scalar(
            update(Campaign)
            .where(Campaign.id == campaign.id)
            .values(event_sequence=Campaign.event_sequence + 1)
            .returning(Campaign.event_sequence)
        )
        if sequence is None:
            raise CampaignNotFoundError(campaign.id)
        row = CampaignEvent(
            id=str(uuid.uuid4()),
            campaign_id=campaign.id,
            sequence=int(sequence),
            event_type=event_type,
            summary=summary,
            payload=payload or {},
            audience_scope=audience_scope,
            branch_id=branch_id,
        )
        session.add(row)
        session.flush()
        return self._info(row)

    def list(
        self, campaign_id: str, *, limit: int = 50, branch_id: str | None = None
    ) -> list[CampaignEventInfo]:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            bound_ids: set[str] = set()
            if branch.head_snapshot_id:
                bound_ids = set(
                    session.scalars(
                        select(SnapshotEventBinding.event_id).where(
                            SnapshotEventBinding.snapshot_id == branch.head_snapshot_id
                        )
                    )
                )
            rows = []
            if bound_ids:
                rows.extend(
                    session.scalars(select(CampaignEvent).where(CampaignEvent.id.in_(bound_ids)))
                )
            rows.extend(
                session.scalars(
                    select(CampaignEvent).where(
                        CampaignEvent.campaign_id == campaign_id,
                        CampaignEvent.branch_id == branch.id,
                        CampaignEvent.committed_snapshot_id.is_(None),
                    )
                )
            )
            rows = sorted(rows, key=lambda row: (row.sequence, row.id))[-max(1, min(limit, 500)) :]
            return [self._info(row) for row in rows]

    @staticmethod
    def _info(row: CampaignEvent) -> CampaignEventInfo:
        return CampaignEventInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            sequence=row.sequence,
            event_type=row.event_type,
            summary=row.summary,
            payload=dict(row.payload),
            audience_scope=row.audience_scope,
            created_at=row.created_at.isoformat(),
        )
