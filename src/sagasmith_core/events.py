"""Campaign-scoped event log."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update

from sagasmith_core.branches import resolve_branch
from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.idempotency import IdempotencyService, IdempotencyWrite
from sagasmith_core.models import (
    ActorKnowledge,
    ActorKnowledgeRevision,
    BranchActorKnowledgeHead,
    Campaign,
    CampaignEvent,
    CampaignEventParticipant,
    Character,
    SnapshotEventBinding,
)
from sagasmith_core.retrieval import lexical_score
from sagasmith_core.visibility import (
    ACTOR_KNOWLEDGE_DISCLOSURE_SCOPES,
    CONTINUITY_AUDIENCES,
    EVENT_AUDIENCE_SCOPES,
    PLAYER_EVENT_AUDIENCE_SCOPES,
    PLAYER_OWNED_ACTOR_DISCLOSURE_SCOPES,
)

EVENT_PARTICIPANT_ROLES = frozenset({"speaker", "listener", "witness", "target"})


@dataclass(frozen=True)
class CampaignEventInfo:
    id: str
    campaign_id: str
    sequence: int
    event_type: str
    summary: str
    retrieval_text: str
    payload: dict[str, Any]
    audience_scope: str
    created_at: str
    participants: tuple[dict[str, str], ...] = ()


class EventService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self,
        campaign_id: str,
        *,
        event_type: str = "narrative",
        summary: str,
        retrieval_text: str | None = None,
        payload: dict[str, Any] | None = None,
        audience_scope: str = "dm",
        participants: list[dict[str, str]] | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> CampaignEventInfo:
        if audience_scope not in EVENT_AUDIENCE_SCOPES:
            raise ValueError(f"invalid event audience scope: {audience_scope}")
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(session, idempotency_key, idempotency_write)
            branch = resolve_branch(session, campaign, branch_id)
            result = self._add_in_session(
                session,
                campaign,
                branch.id,
                event_type=event_type,
                summary=summary,
                retrieval_text=retrieval_text,
                payload=payload,
                audience_scope=audience_scope,
                participants=participants,
            )
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

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
        retrieval_text: str | None = None,
        audience_scope: str = "dm",
        disclosure_scope: str = "owner",
        participants: list[dict[str, str]] | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> tuple[CampaignEventInfo, list[str]]:
        """Append one event and every witnessed knowledge head atomically."""

        if audience_scope not in EVENT_AUDIENCE_SCOPES:
            raise ValueError(f"invalid event audience scope: {audience_scope}")
        if disclosure_scope not in ACTOR_KNOWLEDGE_DISCLOSURE_SCOPES:
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
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(session, idempotency_key, idempotency_write)
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
                        raise ValueError(f"knowledge key already exists for actor: {knowledge_key}")
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
                retrieval_text=retrieval_text,
                payload=payload,
                audience_scope=audience_scope,
                participants=participants,
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
            result = (event_info, knowledge_ids)
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

    def _add_in_session(
        self,
        session,
        campaign: Campaign,
        branch_id: str,
        *,
        event_type: str,
        summary: str,
        retrieval_text: str | None,
        payload: dict[str, Any] | None,
        audience_scope: str,
        participants: list[dict[str, str]] | None = None,
    ) -> CampaignEventInfo:
        if audience_scope not in EVENT_AUDIENCE_SCOPES:
            raise ValueError(f"invalid event audience scope: {audience_scope}")
        normalized_retrieval_text = (
            summary if retrieval_text is None else str(retrieval_text).strip()
        )
        if len(normalized_retrieval_text) > 16_000:
            raise ValueError("event retrieval_text exceeds 16000 characters")
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
            retrieval_text=normalized_retrieval_text,
            payload=payload or {},
            audience_scope=audience_scope,
            branch_id=branch_id,
        )
        session.add(row)
        session.flush()
        normalized_participants = self._normalize_participants(
            session,
            campaign.id,
            participants,
        )
        session.add_all(
            CampaignEventParticipant(
                event_id=row.id,
                actor_id=item["actor_id"],
                role=item["role"],
            )
            for item in normalized_participants
        )
        session.flush()
        return self._info(row, normalized_participants)

    def list(
        self, campaign_id: str, *, limit: int = 50, branch_id: str | None = None
    ) -> list[CampaignEventInfo]:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            rows = self._branch_rows(session, campaign_id, branch)
            rows = rows[-max(1, min(limit, 500)) :]
            participants = self._participant_map(session, [row.id for row in rows])
            return [self._info(row, participants.get(row.id, [])) for row in rows]

    def list_for_actor(
        self,
        campaign_id: str,
        *,
        actor_id: str,
        roles: set[str] | frozenset[str] | None = None,
        limit: int = 50,
        branch_id: str | None = None,
    ) -> list[CampaignEventInfo]:
        """List visible branch events explicitly indexed to one actor."""

        selected_roles = set(roles or EVENT_PARTICIPANT_ROLES)
        unknown_roles = selected_roles - EVENT_PARTICIPANT_ROLES
        if unknown_roles:
            raise ValueError(f"invalid event participant roles: {sorted(unknown_roles)}")
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            actor = session.get(Character, actor_id)
            if actor is None or actor.campaign_id != campaign_id:
                raise LookupError(actor_id)
            branch = resolve_branch(session, campaign, branch_id)
            participant_event_ids = set(
                session.scalars(
                    select(CampaignEventParticipant.event_id).where(
                        CampaignEventParticipant.actor_id == actor_id,
                        CampaignEventParticipant.role.in_(selected_roles),
                    )
                )
            )
            rows = [
                row
                for row in self._branch_rows(session, campaign_id, branch)
                if row.id in participant_event_ids
            ][-max(1, min(limit, 500)) :]
            participants = self._participant_map(session, [row.id for row in rows])
            return [self._info(row, participants.get(row.id, [])) for row in rows]

    def search_for_actor(
        self,
        campaign_id: str,
        *,
        actor_id: str,
        query: str,
        roles: set[str] | frozenset[str] | None = None,
        limit: int = 50,
        branch_id: str | None = None,
    ) -> list[CampaignEventInfo]:
        """Search every branch-visible event explicitly indexed to one actor."""

        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("event search query must not be blank")
        selected_roles = set(roles or EVENT_PARTICIPANT_ROLES)
        unknown_roles = selected_roles - EVENT_PARTICIPANT_ROLES
        if unknown_roles:
            raise ValueError(f"invalid event participant roles: {sorted(unknown_roles)}")
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            actor = session.get(Character, actor_id)
            if actor is None or actor.campaign_id != campaign_id:
                raise LookupError(actor_id)
            branch = resolve_branch(session, campaign, branch_id)
            participant_event_ids = set(
                session.scalars(
                    select(CampaignEventParticipant.event_id).where(
                        CampaignEventParticipant.actor_id == actor_id,
                        CampaignEventParticipant.role.in_(selected_roles),
                    )
                )
            )
            rows = [
                row
                for row in self._branch_rows(session, campaign_id, branch)
                if row.id in participant_event_ids
            ]
            scored = [
                (
                    lexical_score(
                        normalized_query,
                        title=row.event_type,
                        content=row.retrieval_text or row.summary,
                    ),
                    row,
                )
                for row in rows
            ]
            ranked = [
                row
                for score, row in sorted(
                    scored,
                    key=lambda item: (-item[0], -item[1].sequence, item[1].id),
                )
                if score > 0
            ][: max(1, min(limit, 500))]
            participants = self._participant_map(session, [row.id for row in ranked])
            return [self._info(row, participants.get(row.id, [])) for row in ranked]

    def list_for_audience(
        self,
        campaign_id: str,
        *,
        audience: str,
        actor_id: str | None = None,
        limit: int = 50,
        branch_id: str | None = None,
    ) -> list[CampaignEventInfo]:
        """List branch events through the one authoritative audience policy."""

        if audience not in CONTINUITY_AUDIENCES:
            raise ValueError("audience must be 'dm' or 'player'")
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            rows = self._branch_rows(session, campaign_id, branch)
            if audience == "player":
                actor_event_ids: set[str] = set()
                if actor_id:
                    actor = session.get(Character, actor_id)
                    if actor is None or actor.campaign_id != campaign_id:
                        raise LookupError(actor_id)
                    actor_event_ids = {
                        str(source_event_id)
                        for source_event_id in session.scalars(
                            select(ActorKnowledgeRevision.source_event_id)
                            .join(
                                BranchActorKnowledgeHead,
                                BranchActorKnowledgeHead.revision_id == ActorKnowledgeRevision.id,
                            )
                            .join(
                                ActorKnowledge,
                                ActorKnowledge.id == BranchActorKnowledgeHead.knowledge_id,
                            )
                            .where(
                                BranchActorKnowledgeHead.branch_id == branch.id,
                                ActorKnowledge.actor_id == actor_id,
                                ActorKnowledgeRevision.source_event_id.is_not(None),
                                ActorKnowledgeRevision.disclosure_scope.in_(
                                    PLAYER_OWNED_ACTOR_DISCLOSURE_SCOPES
                                ),
                            )
                        )
                    }
                    actor_event_ids.update(
                        session.scalars(
                            select(CampaignEventParticipant.event_id).where(
                                CampaignEventParticipant.actor_id == actor_id,
                                CampaignEventParticipant.role.in_(EVENT_PARTICIPANT_ROLES),
                            )
                        )
                    )
                rows = [
                    row
                    for row in rows
                    if row.audience_scope in PLAYER_EVENT_AUDIENCE_SCOPES
                    or (row.audience_scope == "actor" and row.id in actor_event_ids)
                ]
            rows = rows[-max(1, min(limit, 500)) :]
            participants = self._participant_map(session, [row.id for row in rows])
            return [self._info(row, participants.get(row.id, [])) for row in rows]

    @staticmethod
    def _branch_rows(session, campaign_id: str, branch) -> list[CampaignEvent]:
        bound_ids: set[str] = set()
        if branch.head_snapshot_id:
            bound_ids = set(
                session.scalars(
                    select(SnapshotEventBinding.event_id).where(
                        SnapshotEventBinding.snapshot_id == branch.head_snapshot_id
                    )
                )
            )
        rows: dict[str, CampaignEvent] = {}
        if bound_ids:
            rows.update(
                (row.id, row)
                for row in session.scalars(
                    select(CampaignEvent).where(CampaignEvent.id.in_(bound_ids))
                )
            )
        rows.update(
            (row.id, row)
            for row in session.scalars(
                select(CampaignEvent).where(
                    CampaignEvent.campaign_id == campaign_id,
                    CampaignEvent.branch_id == branch.id,
                    CampaignEvent.committed_snapshot_id.is_(None),
                )
            )
        )
        return sorted(rows.values(), key=lambda row: (row.sequence, row.id))

    @staticmethod
    def _normalize_participants(
        session,
        campaign_id: str,
        participants: list[dict[str, str]] | None,
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for index, raw in enumerate(participants or []):
            if not isinstance(raw, dict):
                raise ValueError(f"event participants[{index}] must be an object")
            unknown = set(raw) - {"actor_id", "role"}
            if unknown:
                raise ValueError(
                    f"event participants[{index}] has unknown fields: {sorted(unknown)}"
                )
            actor_id = str(raw.get("actor_id") or "").strip()
            role = str(raw.get("role") or "").strip()
            if not actor_id:
                raise ValueError(f"event participants[{index}].actor_id is required")
            if role not in EVENT_PARTICIPANT_ROLES:
                raise ValueError(f"invalid event participant role: {role}")
            actor = session.get(Character, actor_id)
            if actor is None or actor.campaign_id != campaign_id:
                raise ValueError("every event participant must be a character in this campaign")
            key = (actor_id, role)
            if key in seen:
                raise ValueError("event participants must be unique by actor and role")
            seen.add(key)
            normalized.append({"actor_id": actor_id, "role": role})
        return sorted(normalized, key=lambda item: (item["role"], item["actor_id"]))

    @staticmethod
    def _participant_map(session, event_ids: list[str]) -> dict[str, list[dict[str, str]]]:
        result: dict[str, list[dict[str, str]]] = {}
        if not event_ids:
            return result
        rows = session.scalars(
            select(CampaignEventParticipant)
            .where(CampaignEventParticipant.event_id.in_(event_ids))
            .order_by(
                CampaignEventParticipant.event_id,
                CampaignEventParticipant.role,
                CampaignEventParticipant.actor_id,
            )
        )
        for row in rows:
            result.setdefault(row.event_id, []).append({"actor_id": row.actor_id, "role": row.role})
        return result

    @staticmethod
    def _info(
        row: CampaignEvent,
        participants: list[dict[str, str]] | None = None,
    ) -> CampaignEventInfo:
        return CampaignEventInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            sequence=row.sequence,
            event_type=row.event_type,
            summary=row.summary,
            retrieval_text=row.retrieval_text,
            payload=dict(row.payload),
            audience_scope=row.audience_scope,
            created_at=row.created_at.isoformat(),
            participants=tuple(dict(item) for item in participants or []),
        )
