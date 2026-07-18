"""Branch-scoped subjective knowledge for campaign actor instances."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

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
from sagasmith_core.retrieval import lexical_score

_STATUSES = {"known", "belief", "rumor", "false_belief", "forgotten", "modified", "superseded"}
_DISCLOSURE_SCOPES = {"dm", "owner", "party", "public", "player"}


@dataclass(frozen=True)
class ActorKnowledgeInfo:
    id: str
    campaign_id: str
    actor_id: str
    knowledge_key: str
    subject_ref: str
    revision_id: str
    proposition: str
    epistemic_status: str
    confidence: int
    source_event_id: str | None
    cause: str
    disclosure_scope: str


class ActorKnowledgeService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self,
        campaign_id: str,
        *,
        actor_id: str,
        knowledge_key: str,
        proposition: str,
        subject_ref: str = "",
        epistemic_status: str = "known",
        confidence: int = 3,
        source_event_id: str | None = None,
        cause: str = "witnessed",
        disclosure_scope: str = "dm",
        branch_id: str | None = None,
    ) -> ActorKnowledgeInfo:
        self._validate_status(epistemic_status)
        self._validate_disclosure_scope(disclosure_scope)
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            actor = session.get(Character, actor_id)
            if actor is None or actor.campaign_id != campaign_id:
                raise ValueError("actor must be a live character in this campaign")
            branch = resolve_branch(session, campaign, branch_id)
            self._validate_event(
                session, source_event_id, campaign_id, branch.id, branch.head_snapshot_id
            )
            existing = session.scalar(
                select(ActorKnowledge).where(
                    ActorKnowledge.actor_id == actor_id,
                    ActorKnowledge.knowledge_key == knowledge_key,
                )
            )
            if existing is not None:
                head = session.get(
                    BranchActorKnowledgeHead,
                    {"branch_id": branch.id, "knowledge_id": existing.id},
                )
                if head is not None:
                    raise ValueError(f"knowledge key already exists for actor: {knowledge_key}")
                if subject_ref and existing.subject_ref and subject_ref != existing.subject_ref:
                    raise ValueError("knowledge key has a different subject on another branch")
                knowledge = existing
            else:
                knowledge = ActorKnowledge(
                    id=str(uuid.uuid4()),
                    campaign_id=campaign_id,
                    actor_id=actor_id,
                    knowledge_key=knowledge_key,
                    subject_ref=subject_ref,
                )
            revision = self._revision(
                knowledge.id,
                proposition=proposition,
                epistemic_status=epistemic_status,
                confidence=confidence,
                source_event_id=source_event_id,
                cause=cause,
                disclosure_scope=disclosure_scope,
            )
            session.add_all([knowledge, revision])
            session.flush()
            session.add(
                BranchActorKnowledgeHead(
                    branch_id=branch.id, knowledge_id=knowledge.id, revision_id=revision.id
                )
            )
            return self._info(knowledge, revision)

    def revise(
        self,
        knowledge_id: str,
        *,
        proposition: str,
        epistemic_status: str = "known",
        confidence: int = 3,
        source_event_id: str | None = None,
        cause: str = "told_by",
        disclosure_scope: str = "dm",
        branch_id: str | None = None,
    ) -> ActorKnowledgeInfo:
        self._validate_status(epistemic_status)
        self._validate_disclosure_scope(disclosure_scope)
        with self.database.transaction() as session:
            knowledge = session.get(ActorKnowledge, knowledge_id)
            if knowledge is None:
                raise LookupError(knowledge_id)
            campaign = session.get(Campaign, knowledge.campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(knowledge.campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            self._validate_event(
                session, source_event_id, campaign.id, branch.id, branch.head_snapshot_id
            )
            head = session.get(
                BranchActorKnowledgeHead,
                {"branch_id": branch.id, "knowledge_id": knowledge.id},
            )
            if head is None:
                raise LookupError(f"knowledge {knowledge_id} is not visible on branch {branch.id}")
            parent_id = head.revision_id
            revision = self._revision(
                knowledge.id,
                parent_id=parent_id,
                proposition=proposition,
                epistemic_status=epistemic_status,
                confidence=confidence,
                source_event_id=source_event_id,
                cause=cause,
                disclosure_scope=disclosure_scope,
            )
            session.add(revision)
            session.flush()
            head.revision_id = revision.id
            return self._info(knowledge, revision)

    def list(
        self, campaign_id: str, *, actor_id: str, branch_id: str | None = None
    ) -> list[ActorKnowledgeInfo]:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            rows = session.execute(
                select(ActorKnowledge, ActorKnowledgeRevision)
                .join(
                    BranchActorKnowledgeHead,
                    BranchActorKnowledgeHead.knowledge_id == ActorKnowledge.id,
                )
                .join(
                    ActorKnowledgeRevision,
                    ActorKnowledgeRevision.id == BranchActorKnowledgeHead.revision_id,
                )
                .where(
                    BranchActorKnowledgeHead.branch_id == branch.id,
                    ActorKnowledge.actor_id == actor_id,
                )
                .order_by(ActorKnowledge.knowledge_key)
            )
            return [self._info(*row) for row in rows]

    def get(self, knowledge_id: str, *, branch_id: str | None = None) -> ActorKnowledgeInfo:
        with self.database.transaction() as session:
            knowledge = session.get(ActorKnowledge, knowledge_id)
            if knowledge is None:
                raise LookupError(knowledge_id)
            campaign = session.get(Campaign, knowledge.campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(knowledge.campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            head = session.get(
                BranchActorKnowledgeHead,
                {"branch_id": branch.id, "knowledge_id": knowledge.id},
            )
            if head is None:
                raise LookupError(knowledge_id)
            revision = session.get(ActorKnowledgeRevision, head.revision_id)
            if revision is None:
                raise LookupError(head.revision_id)
            return self._info(knowledge, revision)

    def search(
        self,
        campaign_id: str,
        *,
        actor_id: str,
        query: str,
        branch_id: str | None = None,
        limit: int = 8,
    ) -> list[ActorKnowledgeInfo]:
        values = self.list(campaign_id, actor_id=actor_id, branch_id=branch_id)
        ranked = sorted(
            values,
            key=lambda value: (
                -lexical_score(query, title=value.knowledge_key, content=value.proposition)
            ),
        )
        return ranked[: max(1, min(limit, 100))]

    @staticmethod
    def _revision(
        knowledge_id: str,
        *,
        proposition: str,
        epistemic_status: str,
        confidence: int,
        source_event_id: str | None,
        cause: str,
        disclosure_scope: str,
        parent_id: str | None = None,
    ) -> ActorKnowledgeRevision:
        return ActorKnowledgeRevision(
            id=str(uuid.uuid4()),
            knowledge_id=knowledge_id,
            parent_id=parent_id,
            proposition=proposition,
            epistemic_status=epistemic_status,
            confidence=max(0, min(confidence, 5)),
            source_event_id=source_event_id,
            cause=cause,
            disclosure_scope=disclosure_scope,
        )

    @staticmethod
    def _validate_status(value: str) -> None:
        if value not in _STATUSES:
            raise ValueError(f"invalid epistemic status: {value}")

    @staticmethod
    def _validate_disclosure_scope(value: str) -> None:
        if value not in _DISCLOSURE_SCOPES:
            raise ValueError(f"invalid actor-knowledge disclosure scope: {value}")

    @staticmethod
    def _validate_event(
        session,
        event_id: str | None,
        campaign_id: str,
        branch_id: str,
        head_snapshot_id: str | None,
    ) -> None:
        if event_id is None:
            return
        event = session.get(CampaignEvent, event_id)
        if event is None or event.campaign_id != campaign_id:
            raise LookupError(event_id)
        visible = event.branch_id == branch_id and event.committed_snapshot_id is None
        if not visible and head_snapshot_id:
            visible = (
                session.get(
                    SnapshotEventBinding,
                    {"snapshot_id": head_snapshot_id, "event_id": event_id},
                )
                is not None
            )
        if not visible:
            raise LookupError(f"event {event_id} is not visible on branch {branch_id}")

    @staticmethod
    def _info(knowledge: ActorKnowledge, revision: ActorKnowledgeRevision) -> ActorKnowledgeInfo:
        return ActorKnowledgeInfo(
            id=knowledge.id,
            campaign_id=knowledge.campaign_id,
            actor_id=knowledge.actor_id,
            knowledge_key=knowledge.knowledge_key,
            subject_ref=knowledge.subject_ref,
            revision_id=revision.id,
            proposition=revision.proposition,
            epistemic_status=revision.epistemic_status,
            confidence=revision.confidence,
            source_event_id=revision.source_event_id,
            cause=revision.cause,
            disclosure_scope=revision.disclosure_scope,
        )
