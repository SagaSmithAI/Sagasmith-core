"""Branch-scoped subjective knowledge for campaign actor instances."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

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
    Character,
    SnapshotEventBinding,
)
from sagasmith_core.retrieval import lexical_score
from sagasmith_core.visibility import ACTOR_KNOWLEDGE_DISCLOSURE_SCOPES

ACTOR_KNOWLEDGE_STATUSES = frozenset(
    {"known", "belief", "rumor", "false_belief", "forgotten", "modified", "superseded"}
)
INACTIVE_ACTOR_KNOWLEDGE_STATUSES = frozenset({"forgotten", "superseded"})


class _UnsetSourceEvent:
    """Distinguish an omitted source event from an explicit evidence clear."""


_UNSET_SOURCE_EVENT = _UnsetSourceEvent()


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
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> ActorKnowledgeInfo:
        self._validate_status(epistemic_status)
        self._validate_disclosure_scope(disclosure_scope)
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
                branch.head_snapshot_id,
                actor_id=actor_id,
                knowledge_key=knowledge_key,
                proposition=proposition,
                subject_ref=subject_ref,
                epistemic_status=epistemic_status,
                confidence=confidence,
                source_event_id=source_event_id,
                cause=cause,
                disclosure_scope=disclosure_scope,
            )
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

    def revise(
        self,
        knowledge_id: str,
        *,
        proposition: str,
        epistemic_status: str | None = None,
        confidence: int | None = None,
        source_event_id: str | None | _UnsetSourceEvent = _UNSET_SOURCE_EVENT,
        cause: str | None = None,
        disclosure_scope: str | None = None,
        branch_id: str | None = None,
        expected_revision_id: str | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> ActorKnowledgeInfo:
        with self.database.transaction() as session:
            knowledge = session.get(ActorKnowledge, knowledge_id)
            if knowledge is None:
                raise LookupError(knowledge_id)
            campaign = session.get(Campaign, knowledge.campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(knowledge.campaign_id)
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(session, idempotency_key, idempotency_write)
            branch = resolve_branch(session, campaign, branch_id)
            result = self._revise_in_session(
                session,
                knowledge,
                branch.id,
                branch.head_snapshot_id,
                proposition=proposition,
                epistemic_status=epistemic_status,
                confidence=confidence,
                source_event_id=source_event_id,
                cause=cause,
                disclosure_scope=disclosure_scope,
                expected_revision_id=expected_revision_id,
            )
            idempotency.remember_write_in_session(
                session,
                campaign_id=knowledge.campaign_id,
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
        head_snapshot_id: str | None,
        *,
        actor_id: str,
        knowledge_key: str,
        proposition: str,
        subject_ref: str,
        epistemic_status: str,
        confidence: int,
        source_event_id: str | None,
        cause: str,
        disclosure_scope: str,
    ) -> ActorKnowledgeInfo:
        self._validate_status(epistemic_status)
        self._validate_disclosure_scope(disclosure_scope)
        actor = session.get(Character, actor_id)
        if actor is None or actor.campaign_id != campaign.id:
            raise ValueError("actor must be a live character in this campaign")
        self._validate_event(session, source_event_id, campaign.id, branch_id, head_snapshot_id)
        existing = session.scalar(
            select(ActorKnowledge).where(
                ActorKnowledge.actor_id == actor_id,
                ActorKnowledge.knowledge_key == knowledge_key,
            )
        )
        if existing is not None:
            head = session.get(
                BranchActorKnowledgeHead,
                {"branch_id": branch_id, "knowledge_id": existing.id},
            )
            if head is not None:
                raise ValueError(f"knowledge key already exists for actor: {knowledge_key}")
            if subject_ref and existing.subject_ref and subject_ref != existing.subject_ref:
                raise ValueError("knowledge key has a different subject on another branch")
            knowledge = existing
        else:
            knowledge = ActorKnowledge(
                id=str(uuid.uuid4()),
                campaign_id=campaign.id,
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
                branch_id=branch_id, knowledge_id=knowledge.id, revision_id=revision.id
            )
        )
        return self._info(knowledge, revision)

    def _revise_in_session(
        self,
        session,
        knowledge: ActorKnowledge,
        branch_id: str,
        head_snapshot_id: str | None,
        *,
        proposition: str,
        epistemic_status: str | None,
        confidence: int | None,
        source_event_id: str | None | _UnsetSourceEvent,
        cause: str | None,
        disclosure_scope: str | None,
        expected_revision_id: str | None,
    ) -> ActorKnowledgeInfo:
        head = session.get(
            BranchActorKnowledgeHead,
            {"branch_id": branch_id, "knowledge_id": knowledge.id},
        )
        if head is None:
            raise LookupError(f"knowledge {knowledge.id} is not visible on branch {branch_id}")
        if expected_revision_id is not None and head.revision_id != expected_revision_id:
            raise ValueError(
                f"expected actor-knowledge revision {expected_revision_id}, "
                f"current revision is {head.revision_id}"
            )
        current = session.get(ActorKnowledgeRevision, head.revision_id)
        if current is None:
            raise LookupError(head.revision_id)
        resolved_status = (
            current.epistemic_status if epistemic_status is None else epistemic_status
        )
        resolved_confidence = current.confidence if confidence is None else confidence
        resolved_source_event_id = (
            current.source_event_id
            if isinstance(source_event_id, _UnsetSourceEvent)
            else source_event_id
        )
        resolved_cause = current.cause if cause is None else cause
        resolved_disclosure_scope = (
            current.disclosure_scope if disclosure_scope is None else disclosure_scope
        )
        self._validate_status(resolved_status)
        self._validate_disclosure_scope(resolved_disclosure_scope)
        self._validate_event(
            session,
            resolved_source_event_id,
            knowledge.campaign_id,
            branch_id,
            head_snapshot_id,
        )
        revision = self._revision(
            knowledge.id,
            parent_id=head.revision_id,
            proposition=proposition,
            epistemic_status=resolved_status,
            confidence=resolved_confidence,
            source_event_id=resolved_source_event_id,
            cause=resolved_cause,
            disclosure_scope=resolved_disclosure_scope,
        )
        session.add(revision)
        session.flush()
        previous_revision_id = head.revision_id
        changed = session.execute(
            update(BranchActorKnowledgeHead)
            .where(
                BranchActorKnowledgeHead.branch_id == branch_id,
                BranchActorKnowledgeHead.knowledge_id == knowledge.id,
                BranchActorKnowledgeHead.revision_id == previous_revision_id,
            )
            .values(revision_id=revision.id),
            execution_options={"synchronize_session": False},
        ).rowcount
        if changed != 1:
            raise ValueError(
                "expected actor-knowledge revision "
                f"{previous_revision_id}, current revision changed concurrently"
            )
        session.expire(head)
        return self._info(knowledge, revision)

    def list(
        self,
        campaign_id: str,
        *,
        actor_id: str,
        branch_id: str | None = None,
        include_inactive: bool = False,
        disclosure_scopes: set[str] | frozenset[str] | None = None,
    ) -> list[ActorKnowledgeInfo]:
        selected_disclosure_scopes = (
            None if disclosure_scopes is None else set(disclosure_scopes)
        )
        if selected_disclosure_scopes is not None:
            unknown_scopes = (
                selected_disclosure_scopes - ACTOR_KNOWLEDGE_DISCLOSURE_SCOPES
            )
            if unknown_scopes:
                raise ValueError(
                    "invalid actor-knowledge disclosure scopes: "
                    f"{sorted(unknown_scopes)}"
                )
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            statement = (
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
            if not include_inactive:
                statement = statement.where(
                    ActorKnowledgeRevision.epistemic_status.not_in(
                        INACTIVE_ACTOR_KNOWLEDGE_STATUSES
                    )
                )
            if selected_disclosure_scopes is not None:
                statement = statement.where(
                    ActorKnowledgeRevision.disclosure_scope.in_(
                        selected_disclosure_scopes
                    )
                )
            rows = session.execute(statement)
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
        offset: int = 0,
        include_inactive: bool = False,
        disclosure_scopes: set[str] | frozenset[str] | None = None,
    ) -> list[ActorKnowledgeInfo]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer between 1 and 500")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 100_000
        ):
            raise ValueError("offset must be an integer between 0 and 100000")
        values = self.list(
            campaign_id,
            actor_id=actor_id,
            branch_id=branch_id,
            include_inactive=include_inactive,
            disclosure_scopes=disclosure_scopes,
        )
        ranked = sorted(
            values,
            key=lambda value: (
                -lexical_score(query, title=value.knowledge_key, content=value.proposition)
            ),
        )
        return ranked[offset : offset + limit]

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
        if value not in ACTOR_KNOWLEDGE_STATUSES:
            raise ValueError(f"invalid epistemic status: {value}")

    @staticmethod
    def _validate_disclosure_scope(value: str) -> None:
        if value not in ACTOR_KNOWLEDGE_DISCLOSURE_SCOPES:
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
