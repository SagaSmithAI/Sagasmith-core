"""Atomic post-scene continuity commits across the durable campaign ledgers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy import select

from sagasmith_core.branches import resolve_branch
from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.events import EventService
from sagasmith_core.knowledge import ActorKnowledgeService
from sagasmith_core.memory import MemoryService
from sagasmith_core.models import (
    ActorKnowledge,
    BranchFactHead,
    Campaign,
    CampaignMemory,
)
from sagasmith_core.snapshots import SnapshotService


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
        snapshot: dict[str, Any] | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            event_info = self.events._add_in_session(
                session,
                campaign,
                branch.id,
                event_type=str(event.get("event_type", "narrative")),
                summary=self._required_text(event, "summary"),
                payload=dict(event.get("payload") or {}),
                audience_scope=str(event.get("audience_scope", "dm")),
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

            return {
                "event": asdict(event_info),
                "facts": [asdict(item) for item in fact_results],
                "actor_knowledge": [asdict(item) for item in knowledge_results],
                "snapshot": asdict(snapshot_result) if snapshot_result is not None else None,
            }

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
        if action not in {"add", "upsert"}:
            raise ValueError(f"unsupported fact action: {action}")
        fact_key = self._required_text(data, "fact_key")
        memory = session.scalar(
            select(CampaignMemory).where(
                CampaignMemory.campaign_id == campaign.id,
                CampaignMemory.fact_key == fact_key,
            )
        )
        if memory is not None:
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
