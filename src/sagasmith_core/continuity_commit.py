"""Atomic post-scene continuity commits across the durable campaign ledgers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy import select

from sagasmith_core.branches import resolve_branch
from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.concurrency import compare_and_swap_campaign
from sagasmith_core.database import Database
from sagasmith_core.events import EventService
from sagasmith_core.idempotency import IdempotencyService, IdempotencyWrite, request_hash
from sagasmith_core.knowledge import ActorKnowledgeService
from sagasmith_core.memory import MemoryService
from sagasmith_core.models import (
    ActorKnowledge,
    BranchFactHead,
    Campaign,
    CampaignMemory,
)
from sagasmith_core.modules import ModuleService
from sagasmith_core.snapshots import SnapshotService
from sagasmith_core.state import CharacterStateUpdate, StateMutationService

FACT_KEY_WRITE_ACTIONS = frozenset({"add", "upsert"})


class ContinuityCommitService:
    """Persist one narrative outcome without exposing partially saved continuity."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.events = EventService(database)
        self.facts = MemoryService(database)
        self.knowledge = ActorKnowledgeService(database)

    def commit(
        self,
        campaign_id: str,
        *,
        event: dict[str, Any],
        facts: list[dict[str, Any]] | None = None,
        actor_knowledge: list[dict[str, Any]] | None = None,
        campaign_state: dict[str, Any] | None = None,
        character_updates: list[CharacterStateUpdate] | None = None,
        scene_progress_updates: list[dict[str, Any]] | None = None,
        expected_campaign_revision: int | None = None,
        operation: str = "continuity.commit",
        actor: str = "runtime",
        rule_receipts: list[dict[str, Any]] | None = None,
        snapshot: dict[str, Any] | None = None,
        branch_id: str | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(session, idempotency_key, idempotency_write)
            branch = resolve_branch(session, campaign, branch_id)
            updates = list(character_updates or [])
            progress_updates = list(scene_progress_updates or [])
            receipts = list(rule_receipts or [])
            active_branch = campaign.active_branch_id == branch.id
            if not active_branch and (
                campaign_state is not None or updates or progress_updates or receipts
            ):
                raise ValueError(
                    "state, progress, and rule receipts can be settled only on the active branch"
                )
            revision_rows = []
            has_state_mutation = campaign_state is not None or bool(updates) or bool(receipts)
            if active_branch and has_state_mutation:
                revision_rows = StateMutationService(self.database).replace(
                    campaign.id,
                    campaign_state=(
                        dict(campaign.state) if campaign_state is None else dict(campaign_state)
                    ),
                    character_updates=updates,
                    expected_campaign_revision=(
                        campaign.revision
                        if expected_campaign_revision is None
                        else expected_campaign_revision
                    ),
                    operation=operation,
                    actor=actor,
                    branch_id=branch.id,
                    idempotency_key=idempotency_key,
                    idempotency_request_hash=(
                        request_hash(idempotency_write.payload)
                        if idempotency_write is not None
                        else None
                    ),
                    rule_receipts=receipts,
                    reversible=False,
                ) or []
                session.expire(campaign)
                session.refresh(campaign)
            else:
                compare_and_swap_campaign(
                    session,
                    campaign.id,
                    expected_revision=(
                        campaign.revision
                        if expected_campaign_revision is None
                        else expected_campaign_revision
                    ),
                    expected_branch_id=branch.id if active_branch else None,
                    advance_revision=False,
                )
                session.expire(campaign)
                session.refresh(campaign)

            progress_results = []
            modules = ModuleService(self.database)
            for progress_update in progress_updates:
                unknown = set(progress_update) - {
                    "scene_id",
                    "status",
                    "progress",
                    "state",
                    "current_room",
                    "current_location_key",
                    "scope_id",
                    "expected_state_version",
                    "spatial_review",
                }
                if unknown:
                    raise ValueError(
                        "unsupported scene progress fields: " + ", ".join(sorted(unknown))
                    )
                progress_results.append(
                    modules.set_scene_progress(
                        campaign_id=campaign.id,
                        **dict(progress_update),
                    )
                )
            event_info = self.events._add_in_session(
                session,
                campaign,
                branch.id,
                event_type=str(event.get("event_type", "narrative")),
                summary=self._required_text(event, "summary"),
                payload=dict(event.get("payload") or {}),
                audience_scope=str(event.get("audience_scope", "dm")),
                participants=list(event.get("participants") or []),
            )

            fact_results = [
                self._apply_fact(session, campaign, branch.id, event_info.id, dict(item))
                for item in facts or []
            ]
            knowledge_results = [
                self._apply_knowledge(
                    session,
                    campaign,
                    branch.id,
                    branch.head_snapshot_id,
                    event_info.id,
                    dict(item),
                )
                for item in actor_knowledge or []
            ]
            session.flush()

            snapshot_result = None
            if snapshot is not None:
                if campaign.active_branch_id != branch.id:
                    raise ValueError("continuity commit can snapshot only the checked-out branch")
                snapshot_data = dict(snapshot)
                snapshot_result = SnapshotService(self.database)._create_in_session(
                    session,
                    campaign,
                    label=str(snapshot_data.get("label", "Continuity commit")),
                    recap=(
                        dict(snapshot_data["recap"])
                        if snapshot_data.get("recap") is not None
                        else None
                    ),
                    parent_id=snapshot_data.get("parent_id"),
                )

            result = {
                "event": asdict(event_info),
                "facts": [asdict(item) for item in fact_results],
                "actor_knowledge": [asdict(item) for item in knowledge_results],
                "campaign_revision": campaign.revision,
                "state_revisions": [asdict(item) for item in revision_rows],
                "scene_progress": progress_results,
                "snapshot": asdict(snapshot_result) if snapshot_result is not None else None,
            }
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
                mutation_group_id=(
                    revision_rows[0].mutation_group_id if revision_rows else None
                ),
            )
            return result

    def _apply_fact(
        self,
        session,
        campaign: Campaign,
        branch_id: str,
        event_id: str,
        data: dict[str, Any],
    ):
        action = str(data.pop("action", "upsert"))
        content = self._required_text(data, "content")
        source_event_ids = list(data.pop("source_event_ids", None) or [event_id])
        if action == "revise":
            memory_id = self._required_text(data, "memory_id")
            memory = session.get(CampaignMemory, memory_id)
            if memory is None or memory.campaign_id != campaign.id:
                raise LookupError(memory_id)
            return self.facts._revise_in_session(
                session,
                memory,
                branch_id,
                content=content,
                metadata=data.get("metadata"),
                snapshot_id=data.get("snapshot_id"),
                expected_revision_id=data.get("expected_revision_id"),
                status=data.get("status"),
                valid_from=data.get("valid_from"),
                valid_to=data.get("valid_to"),
                source_event_ids=source_event_ids,
                importance=data.get("importance"),
                disclosure_scope=data.get("disclosure_scope"),
            )
        if action not in FACT_KEY_WRITE_ACTIONS:
            raise ValueError(f"unsupported fact action: {action}")
        fact_key = self._required_text(data, "fact_key")
        memory = session.scalar(
            select(CampaignMemory).where(
                CampaignMemory.campaign_id == campaign.id,
                CampaignMemory.fact_key == fact_key,
            )
        )
        if memory is not None:
            self.facts._require_existing_fact_identity(
                memory,
                {
                    field: data[field]
                    for field in ("kind", "subject", "subject_ref", "predicate")
                    if field in data
                },
            )
            head = session.get(
                BranchFactHead,
                {"branch_id": branch_id, "memory_id": memory.id},
            )
            if action == "add" and head is not None:
                raise ValueError(f"campaign fact already exists: {fact_key}")
            if head is None:
                if data.get("expected_revision_id") is not None:
                    raise ValueError("expected revision cannot target a missing branch fact")
                return self.facts._add_branch_revision_in_session(
                    session,
                    memory,
                    branch_id,
                    content=content,
                    metadata=data.get("metadata"),
                    snapshot_id=data.get("snapshot_id"),
                    status=str(data.get("status", "active")),
                    valid_from=data.get("valid_from"),
                    valid_to=data.get("valid_to"),
                    source_event_ids=source_event_ids,
                    importance=int(data.get("importance", 3)),
                    disclosure_scope=data.get("disclosure_scope"),
                )
            return self.facts._revise_in_session(
                session,
                memory,
                branch_id,
                content=content,
                metadata=data.get("metadata"),
                snapshot_id=data.get("snapshot_id"),
                expected_revision_id=data.get("expected_revision_id"),
                status=str(data.get("status", "active")),
                valid_from=data.get("valid_from"),
                valid_to=data.get("valid_to"),
                source_event_ids=source_event_ids,
                importance=data.get("importance", 3),
                disclosure_scope=data.get("disclosure_scope"),
            )
        if data.get("expected_revision_id") is not None:
            raise ValueError("expected revision cannot target a missing fact")
        return self.facts._add_in_session(
            session,
            campaign.id,
            branch_id,
            content=content,
            kind=str(data.get("kind", "fact")),
            subject=str(data.get("subject", "")),
            metadata=data.get("metadata"),
            snapshot_id=data.get("snapshot_id"),
            fact_key=fact_key,
            subject_ref=str(data.get("subject_ref", "")),
            predicate=str(data.get("predicate", "")),
            status=str(data.get("status", "active")),
            valid_from=data.get("valid_from"),
            valid_to=data.get("valid_to"),
            source_event_ids=source_event_ids,
            importance=int(data.get("importance", 3)),
            disclosure_scope=data.get("disclosure_scope"),
        )

    def _apply_knowledge(
        self,
        session,
        campaign: Campaign,
        branch_id: str,
        head_snapshot_id: str | None,
        event_id: str,
        data: dict[str, Any],
    ):
        action = str(data.pop("action", "add"))
        source_event_id = data.get("source_event_id") or event_id
        if action == "add":
            return self.knowledge._add_in_session(
                session,
                campaign,
                branch_id,
                head_snapshot_id,
                actor_id=self._required_text(data, "actor_id"),
                knowledge_key=self._required_text(data, "knowledge_key"),
                proposition=self._required_text(data, "proposition"),
                subject_ref=str(data.get("subject_ref", "")),
                epistemic_status=str(data.get("epistemic_status", "known")),
                confidence=int(data.get("confidence", 3)),
                source_event_id=str(source_event_id),
                cause=str(data.get("cause", "witnessed")),
                disclosure_scope=str(data.get("disclosure_scope", "dm")),
            )
        if action != "revise":
            raise ValueError(f"unsupported actor-knowledge action: {action}")
        knowledge_id = self._required_text(data, "knowledge_id")
        knowledge = session.get(ActorKnowledge, knowledge_id)
        if knowledge is None or knowledge.campaign_id != campaign.id:
            raise LookupError(knowledge_id)
        return self.knowledge._revise_in_session(
            session,
            knowledge,
            branch_id,
            head_snapshot_id,
            proposition=self._required_text(data, "proposition"),
            epistemic_status=str(data.get("epistemic_status", "known")),
            confidence=int(data.get("confidence", 3)),
            source_event_id=str(source_event_id),
            cause=str(data.get("cause", "told_by")),
            disclosure_scope=str(data.get("disclosure_scope", "dm")),
            expected_revision_id=data.get("expected_revision_id"),
        )

    @staticmethod
    def _required_text(data: dict[str, Any], key: str) -> str:
        value = str(data.get(key, "")).strip()
        if not value:
            raise ValueError(f"{key} is required")
        return value
