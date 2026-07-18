"""Safe branch-aware context assembly for D&D agents and narrators."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sagasmith_core.branches import BranchService
from sagasmith_core.database import Database
from sagasmith_core.events import EventService
from sagasmith_core.knowledge import ActorKnowledgeService
from sagasmith_core.memory import MemoryService
from sagasmith_core.models import CampaignSnapshot
from sagasmith_core.modules import ModuleService
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
                if item.metadata.get("disclosure_scope", "dm") in {"public", "party", "player"}
            ]
            knowledge = [
                item
                for item in knowledge
                if item.disclosure_scope in {"owner", "party", "public", "player"}
            ]
            actor_event_ids = {
                item.source_event_id for item in knowledge if item.source_event_id is not None
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
        return {
            "campaign_id": campaign_id,
            "branch": asdict(branch),
            "facts": [asdict(item) for item in facts],
            "events": [asdict(item) for item in events],
            "actor_knowledge": [asdict(item) for item in knowledge],
            "scoped_scene": scoped_state,
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
