"""Safe branch-aware context assembly for D&D agents and narrators."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from sqlalchemy import select

from sagasmith_core.branches import BranchService
from sagasmith_core.database import Database
from sagasmith_core.events import EventService
from sagasmith_core.knowledge import ActorKnowledgeService
from sagasmith_core.memory import MemoryService
from sagasmith_core.models import (
    ActorKnowledge,
    ActorKnowledgeRevision,
    BranchActorKnowledgeHead,
    BranchFactHead,
    CampaignEvent,
    CampaignMemory,
    CampaignSnapshot,
    MemoryRevision,
)
from sagasmith_core.modules import ModuleService
from sagasmith_core.retrieval import lexical_score
from sagasmith_core.snapshots import SnapshotService


class ContinuityService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.branches = BranchService(database)
        self.events = EventService(database)
        self.facts = MemoryService(database)
        self.knowledge = ActorKnowledgeService(database)
        self.modules = ModuleService(database)

    def context(
        self,
        campaign_id: str,
        *,
        query: str = "",
        branch_id: str | None = None,
        actor_id: str | None = None,
        scope_id: str = "party",
        audience: str = "dm",
        limit: int = 8,
        budget_chars: int = 12_000,
    ) -> dict[str, Any]:
        if audience not in {"dm", "player"}:
            raise ValueError("audience must be 'dm' or 'player'")
        branch = (
            self.branches.current(campaign_id)
            if branch_id is None
            else self.branches.get(campaign_id, branch_id)
        )
        facts = self.facts.search(campaign_id, query or " ", limit=limit, branch_id=branch.id)
        events = self.events.list(campaign_id, limit=limit, branch_id=branch.id)
        knowledge = []
        if actor_id:
            knowledge = self.knowledge.search(
                campaign_id,
                actor_id=actor_id,
                query=query or " ",
                branch_id=branch.id,
                limit=limit,
            )
        if audience == "player":
            facts = [
                item
                for item in facts
                if item.disclosure_scope in {"public", "party", "player"}
            ]
            knowledge = [
                item
                for item in knowledge
                if item.disclosure_scope in {"owner", "party", "public", "player"}
            ]
            # Authorization must not depend on which knowledge items happened to
            # rank in the response's top-N window.  Use the actor's complete active
            # branch view to decide whether an actor-scoped event is visible.
            actor_event_ids = set()
            if actor_id:
                actor_event_ids = {
                    item.source_event_id
                    for item in self.knowledge.list(
                        campaign_id, actor_id=actor_id, branch_id=branch.id
                    )
                    if item.source_event_id is not None
                    and item.disclosure_scope in {"owner", "party", "public", "player"}
                }
            events = [
                item
                for item in events
                if item.audience_scope in {"public", "party", "player"}
                or (item.audience_scope == "actor" and item.id in actor_event_ids)
            ]
        current = self.branches.current(campaign_id)
        if branch.id == current.id:
            scoped_state = self.modules.current_scene(campaign_id, scope_id=scope_id)
        else:
            scoped_state = self._snapshot_scope(branch.head_snapshot_id, scope_id)
        fact_values = [asdict(item) for item in facts]
        event_values = [asdict(item) for item in events]
        knowledge_values = [asdict(item) for item in knowledge]
        selected, retrieval = self._apply_budget(
            query=query,
            facts=fact_values,
            events=event_values,
            knowledge=knowledge_values,
            budget_chars=budget_chars,
        )
        return {
            "campaign_id": campaign_id,
            "branch": asdict(branch),
            "facts": selected["facts"],
            "events": selected["events"],
            "actor_knowledge": selected["actor_knowledge"],
            "scoped_scene": scoped_state,
            "retrieval": retrieval,
        }

    def diagnostics(
        self,
        campaign_id: str,
        *,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Return content-free continuity health metrics for operators."""
        branch = (
            self.branches.current(campaign_id)
            if branch_id is None
            else self.branches.get(campaign_id, branch_id)
        )
        with self.database.transaction() as session:
            fact_rows = list(
                session.execute(
                    select(CampaignMemory, MemoryRevision)
                    .join(BranchFactHead, BranchFactHead.memory_id == CampaignMemory.id)
                    .join(MemoryRevision, MemoryRevision.id == BranchFactHead.revision_id)
                    .where(BranchFactHead.branch_id == branch.id)
                )
            )
            knowledge_rows = list(
                session.execute(
                    select(ActorKnowledge, ActorKnowledgeRevision)
                    .join(
                        BranchActorKnowledgeHead,
                        BranchActorKnowledgeHead.knowledge_id == ActorKnowledge.id,
                    )
                    .join(
                        ActorKnowledgeRevision,
                        ActorKnowledgeRevision.id == BranchActorKnowledgeHead.revision_id,
                    )
                    .where(BranchActorKnowledgeHead.branch_id == branch.id)
                )
            )
            events = list(
                session.scalars(
                    select(CampaignEvent).where(
                        CampaignEvent.campaign_id == campaign_id,
                        CampaignEvent.branch_id == branch.id,
                    )
                )
            )
            event_ids = set(
                session.scalars(
                    select(CampaignEvent.id).where(CampaignEvent.campaign_id == campaign_id)
                )
            )
            snapshots = list(
                session.scalars(
                    select(CampaignSnapshot)
                    .where(
                        CampaignSnapshot.campaign_id == campaign_id,
                        CampaignSnapshot.branch_id == branch.id,
                    )
                    .order_by(CampaignSnapshot.slot)
                )
            )

        active_facts = sum(revision.status == "active" for _, revision in fact_rows)
        inactive_knowledge = sum(
            revision.epistemic_status in {"forgotten", "superseded"}
            for _, revision in knowledge_rows
        )
        orphan_fact_sources = sum(
            source_id not in event_ids
            for _, revision in fact_rows
            for source_id in revision.source_event_ids
        )
        orphan_knowledge_sources = sum(
            bool(revision.source_event_id and revision.source_event_id not in event_ids)
            for _, revision in knowledge_rows
        )
        latest = snapshots[-1] if snapshots else None
        return {
            "campaign_id": campaign_id,
            "branch_id": branch.id,
            "facts": {
                "total": len(fact_rows),
                "active": active_facts,
                "inactive": len(fact_rows) - active_facts,
                "orphan_source_event_refs": orphan_fact_sources,
            },
            "actor_knowledge": {
                "total": len(knowledge_rows),
                "active": len(knowledge_rows) - inactive_knowledge,
                "inactive": inactive_knowledge,
                "orphan_source_event_refs": orphan_knowledge_sources,
            },
            "events": {
                "total_on_branch": len(events),
                "unsnapshotted": sum(item.committed_snapshot_id is None for item in events),
                "latest_sequence": max((item.sequence for item in events), default=0),
            },
            "snapshots": {
                "total_on_branch": len(snapshots),
                "latest_id": latest.id if latest else None,
                "latest_slot": latest.slot if latest else None,
                "latest_payload_chars": (
                    len(json.dumps(latest.payload, ensure_ascii=False, separators=(",", ":")))
                    if latest
                    else 0
                ),
            },
        }

    @staticmethod
    def _apply_budget(
        *,
        query: str,
        facts: list[dict[str, Any]],
        events: list[dict[str, Any]],
        knowledge: list[dict[str, Any]],
        budget_chars: int,
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
        budget = max(1_000, min(int(budget_chars), 100_000))
        candidates: list[tuple[float, str, int, dict[str, Any]]] = []
        for index, item in enumerate(facts):
            score = lexical_score(
                query or " ",
                title=" ".join(
                    str(item.get(key) or "")
                    for key in ("fact_key", "subject", "subject_ref", "predicate")
                ),
                content=str(item.get("content") or ""),
            ) + int(item.get("importance") or 3) / 20
            candidates.append((score, "facts", index, item))
        for index, item in enumerate(knowledge):
            score = lexical_score(
                query or " ",
                title=" ".join(
                    str(item.get(key) or "")
                    for key in ("knowledge_key", "subject_ref", "epistemic_status")
                ),
                content=str(item.get("proposition") or ""),
            ) + int(item.get("confidence") or 3) / 20
            candidates.append((score, "actor_knowledge", index, item))
        for index, item in enumerate(events):
            score = lexical_score(
                query or " ",
                title=str(item.get("event_type") or ""),
                content=str(item.get("summary") or ""),
            ) + (index + 1) / max(1, len(events)) / 10
            candidates.append((score, "events", index, item))
        candidates.sort(key=lambda value: (-value[0], value[1], value[2]))

        selected: dict[str, list[dict[str, Any]]] = {
            "facts": [],
            "events": [],
            "actor_knowledge": [],
        }
        used = 0
        for _score, ledger, _index, item in candidates:
            size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            if used and used + size > budget:
                continue
            selected[ledger].append(item)
            used += size
        selected["events"].sort(
            key=lambda item: (item.get("sequence", 0), item.get("id", ""))
        )
        returned = sum(len(values) for values in selected.values())
        return selected, {
            "strategy": "lexical_structured_shared_budget_v2",
            "query": query,
            "budget_chars": budget,
            "used_chars": used,
            "candidate_count": len(candidates),
            "returned_count": returned,
            "truncated": returned < len(candidates),
        }

    def _snapshot_scope(self, snapshot_id: str | None, scope_id: str) -> dict[str, Any] | None:
        if snapshot_id is None:
            return None
        with self.database.transaction() as session:
            snapshot = session.get(CampaignSnapshot, snapshot_id)
            if snapshot is None:
                return None
            SnapshotService._assert_integrity(session, snapshot)
            values = snapshot.payload.get("scene_progress", [])
            for effective_scope in (scope_id, "party"):
                match = next(
                    (
                        item
                        for item in values
                        if item.get("scope_id", "party") == effective_scope
                        and item.get("status") == "current"
                    ),
                    None,
                )
                if match is not None:
                    return {**match, "requested_scope_id": scope_id}
        return None
