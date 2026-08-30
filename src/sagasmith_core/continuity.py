"""Safe branch-aware context assembly for TTRPG agents and narrators."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from sqlalchemy import select

from sagasmith_core.branches import BranchService
from sagasmith_core.context_anchors import (
    CONTEXT_ANCHOR_KIND,
    MAX_PINNED_MODULE_EVIDENCE_CHARS,
    normalize_context_anchor_metadata,
    normalize_context_entity_ref,
    resolve_context_source_binding,
)
from sagasmith_core.database import Database
from sagasmith_core.events import EventService
from sagasmith_core.knowledge import (
    INACTIVE_ACTOR_KNOWLEDGE_STATUSES,
    ActorKnowledgeService,
)
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
from sagasmith_core.visibility import (
    CONTINUITY_AUDIENCES,
    PLAYER_MEMORY_DISCLOSURE_SCOPES,
    PLAYER_MODULE_VISIBILITY_SCOPES,
    PLAYER_OWNED_ACTOR_DISCLOSURE_SCOPES,
)


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
        offset: int = 0,
        budget_chars: int = 12_000,
        related_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        if audience not in CONTINUITY_AUDIENCES:
            raise ValueError("audience must be 'dm' or 'player'")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 100_000
        ):
            raise ValueError("offset must be an integer between 0 and 100000")
        branch = (
            self.branches.current(campaign_id)
            if branch_id is None
            else self.branches.get(campaign_id, branch_id)
        )
        anchors = self.facts.list(
            campaign_id,
            kind=CONTEXT_ANCHOR_KIND,
            branch_id=branch.id,
        )
        fact_page = self.facts.search(
            campaign_id,
            query or " ",
            limit=limit + 1,
            offset=offset,
            branch_id=branch.id,
            excluded_kinds={CONTEXT_ANCHOR_KIND},
            disclosure_scopes=(
                PLAYER_MEMORY_DISCLOSURE_SCOPES if audience == "player" else None
            ),
        )
        event_page = self.events.list_for_audience(
            campaign_id,
            audience=audience,
            actor_id=actor_id,
            limit=limit + 1,
            offset=offset,
            branch_id=branch.id,
        )
        knowledge_page = []
        if actor_id:
            knowledge_page = self.knowledge.search(
                campaign_id,
                actor_id=actor_id,
                query=query or " ",
                branch_id=branch.id,
                limit=limit + 1,
                offset=offset,
                disclosure_scopes=(
                    PLAYER_OWNED_ACTOR_DISCLOSURE_SCOPES
                    if audience == "player"
                    else None
                ),
            )
        facts = fact_page[:limit]
        events = event_page[-limit:]
        knowledge = knowledge_page[:limit]
        stream_has_more = {
            "facts": len(fact_page) > limit,
            "events": len(event_page) > limit,
            "actor_knowledge": len(knowledge_page) > limit,
        }
        current = self.branches.current(campaign_id)
        if branch.id == current.id:
            scoped_state = self.modules.current_scene(campaign_id, scope_id=scope_id)
        else:
            scoped_state = self._snapshot_scope(branch.head_snapshot_id, scope_id)
        if audience == "player":
            scoped_state = self._player_scene_projection(scoped_state)
        active_refs = self._active_context_refs(
            actor_id=actor_id,
            scoped_state=scoped_state,
            related_refs=related_refs,
        )
        module_evidence = (
            self._module_evidence(
                campaign_id,
                anchors=anchors,
                active_refs=active_refs,
            )
            if audience == "dm"
            else []
        )
        fact_values = [asdict(item) for item in facts]
        event_values = [asdict(item) for item in events]
        knowledge_values = [asdict(item) for item in knowledge]
        selected, retrieval = self._apply_budget(
            query=query,
            facts=fact_values,
            events=event_values,
            knowledge=knowledge_values,
            budget_chars=budget_chars,
            reserved_chars=(
                len(
                    json.dumps(
                        module_evidence,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                if module_evidence
                else 0
            ),
        )
        if module_evidence:
            retrieval["strategy"] = "lexical_structured_pinned_module_evidence_v3"
        retrieval["active_context_refs"] = sorted(active_refs)
        retrieval["pinned_module_evidence_count"] = len(module_evidence)
        has_more = any(stream_has_more.values())
        retrieval["pagination"] = {
            "offset": offset,
            "page_limit": limit,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
            "streams": {
                "facts": {
                    "candidate_count": len(facts),
                    "has_more": stream_has_more["facts"],
                },
                "events": {
                    "candidate_count": len(events),
                    "has_more": stream_has_more["events"],
                },
                "actor_knowledge": {
                    "candidate_count": len(knowledge),
                    "has_more": stream_has_more["actor_knowledge"],
                },
            },
        }
        return {
            "campaign_id": campaign_id,
            "branch": asdict(branch),
            "facts": selected["facts"],
            "events": selected["events"],
            "actor_knowledge": selected["actor_knowledge"],
            "module_evidence": module_evidence,
            "scoped_scene": scoped_state,
            "retrieval": retrieval,
        }

    @staticmethod
    def _player_scene_projection(scene: dict[str, Any] | None) -> dict[str, Any] | None:
        """Remove restricted prose and arbitrary progress state from a player context."""

        if not isinstance(scene, dict):
            return None
        progress = dict(scene.get("progress") or {})
        safe_progress = {
            key: progress[key] for key in ("status", "percent", "state_version") if key in progress
        }
        if scene.get("visibility", "restricted") not in PLAYER_MODULE_VISIBILITY_SCOPES:
            return {
                "campaign_id": scene.get("campaign_id"),
                "scope_id": scene.get("scope_id"),
                "requested_scope_id": scene.get("requested_scope_id"),
                "inherited_from_party": scene.get("inherited_from_party", False),
                "scene_id": scene.get("scene_id"),
                "visibility": scene.get("visibility", "restricted"),
                "redacted": True,
                "content": "[Restricted scene content hidden]",
                "progress": safe_progress,
            }
        projected = deepcopy(scene)
        projected["progress"] = safe_progress
        spatial = dict(projected.get("spatial") or {})
        spatial.pop("review", None)
        projected["spatial"] = spatial
        return projected

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
            latest = snapshots[-1] if snapshots else None
            latest_payload_chars = (
                len(
                    json.dumps(
                        SnapshotService._materialize(latest),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                if latest
                else 0
            )

        active_facts = sum(revision.status == "active" for _, revision in fact_rows)
        inactive_knowledge = sum(
            revision.epistemic_status in INACTIVE_ACTOR_KNOWLEDGE_STATUSES
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
                "latest_payload_chars": latest_payload_chars,
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
        reserved_chars: int = 0,
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
        budget = max(1_000, min(int(budget_chars), 100_000))
        reserved = max(0, int(reserved_chars))
        if reserved > MAX_PINNED_MODULE_EVIDENCE_CHARS:
            raise ValueError(
                "pinned module evidence exceeds the context safety cap; narrow related_refs"
            )
        available = max(0, budget - reserved)
        candidates: list[tuple[float, str, int, dict[str, Any]]] = []
        for index, item in enumerate(facts):
            score = (
                lexical_score(
                    query or " ",
                    title=" ".join(
                        str(item.get(key) or "")
                        for key in ("fact_key", "subject", "subject_ref", "predicate")
                    ),
                    content=str(item.get("content") or ""),
                )
                + int(item.get("importance") or 3) / 20
            )
            candidates.append((score, "facts", index, item))
        for index, item in enumerate(knowledge):
            score = (
                lexical_score(
                    query or " ",
                    title=" ".join(
                        str(item.get(key) or "")
                        for key in ("knowledge_key", "subject_ref", "epistemic_status")
                    ),
                    content=str(item.get("proposition") or ""),
                )
                + int(item.get("confidence") or 3) / 20
            )
            candidates.append((score, "actor_knowledge", index, item))
        for index, item in enumerate(events):
            score = (
                lexical_score(
                    query or " ",
                    title=str(item.get("event_type") or ""),
                    content=str(item.get("retrieval_text") or item.get("summary") or ""),
                )
                + (index + 1) / max(1, len(events)) / 10
            )
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
            if used + size > available:
                continue
            selected[ledger].append(item)
            used += size
        selected["events"].sort(key=lambda item: (item.get("sequence", 0), item.get("id", "")))
        returned = sum(len(values) for values in selected.values())
        return selected, {
            "strategy": "lexical_structured_shared_budget_v2",
            "query": query,
            "budget_chars": budget,
            "used_chars": used + reserved,
            "structured_ledger_chars": used,
            "pinned_module_evidence_chars": reserved,
            "pinned_budget_overflow": reserved > budget,
            "candidate_count": len(candidates),
            "returned_count": returned,
            "truncated": returned < len(candidates),
        }

    @staticmethod
    def _active_context_refs(
        *,
        actor_id: str | None,
        scoped_state: dict[str, Any] | None,
        related_refs: list[str] | None,
    ) -> set[str]:
        active = {
            normalize_context_entity_ref(item, field="related_refs[]")
            for item in list(related_refs or [])
        }
        if actor_id:
            active.add(
                normalize_context_entity_ref(
                    f"actor:{actor_id}",
                    field="actor_id",
                )
            )
        if isinstance(scoped_state, dict):
            scene_id = str(scoped_state.get("scene_id") or scoped_state.get("id") or "").strip()
            module_id = str(scoped_state.get("module_id") or "").strip()
            if scene_id:
                active.add(f"scene:{scene_id}")
            if module_id:
                active.add(f"module:{module_id}")
        return active

    def _module_evidence(
        self,
        campaign_id: str,
        *,
        anchors: list[Any],
        active_refs: set[str],
    ) -> list[dict[str, Any]]:
        if not active_refs:
            return []
        by_binding: dict[tuple[str, str], dict[str, Any]] = {}
        for anchor in sorted(anchors, key=lambda item: (item.fact_key, item.id)):
            metadata = normalize_context_anchor_metadata(
                anchor.metadata,
                subject_ref=anchor.subject_ref,
                predicate=anchor.predicate,
                disclosure_scope=anchor.disclosure_scope,
            )
            matched = sorted(active_refs & set(metadata["related_refs"]))
            if not matched:
                continue
            for binding in metadata["source_bindings"]:
                expanded = self.modules.expand(binding["source_ref"]["chunk_id"])
                resolved = resolve_context_source_binding(
                    binding,
                    expanded=expanded,
                    campaign_id=campaign_id,
                )
                key = (
                    resolved["source_ref"]["chunk_id"],
                    resolved["source_excerpt"],
                )
                existing = by_binding.get(key)
                if existing is None:
                    by_binding[key] = {
                        "pinned": True,
                        "context_role": "non_executable_module_evidence",
                        "anchor_fact_keys": [anchor.fact_key],
                        "matched_refs": matched,
                        "source_ref": resolved["source_ref"],
                        "source_excerpt": resolved["source_excerpt"],
                        "module": dict(expanded.get("module") or {}),
                        "chapter": dict(expanded.get("chapter") or {}),
                        "scene": dict(expanded.get("scene") or {}),
                    }
                    continue
                existing["anchor_fact_keys"] = sorted(
                    {*existing["anchor_fact_keys"], anchor.fact_key}
                )
                existing["matched_refs"] = sorted({*existing["matched_refs"], *matched})
        return sorted(
            by_binding.values(),
            key=lambda item: (
                item["source_ref"]["module_id"],
                item["source_ref"]["scene_id"],
                item["source_ref"]["chunk_id"],
                item["source_excerpt"],
            ),
        )

    def _snapshot_scope(self, snapshot_id: str | None, scope_id: str) -> dict[str, Any] | None:
        if snapshot_id is None:
            return None
        with self.database.transaction() as session:
            snapshot = session.get(CampaignSnapshot, snapshot_id)
            if snapshot is None:
                return None
            SnapshotService._assert_integrity(session, snapshot)
            values = SnapshotService._materialize(snapshot).get("scene_progress", [])
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
