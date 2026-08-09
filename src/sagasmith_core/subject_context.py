"""System-neutral subject-scoped decision context over branch-aware memory."""

from __future__ import annotations

from sagasmith_core.context_anchors import normalize_context_entity_ref
from sagasmith_core.database import Database
from sagasmith_core.memory import (
    MemoryInfo,
    MemoryService,
)

SUBJECT_CONTEXT_READ_KINDS = {
    "actor": frozenset({"actor_state"}),
    "faction": frozenset({"faction_state", "faction_knowledge"}),
}


class SubjectContextService:
    """Read only the state/knowledge owned by one actor or faction subject."""

    def __init__(self, database: Database) -> None:
        self.memory = MemoryService(database)

    def list(
        self,
        campaign_id: str,
        *,
        subject_ref: str,
        branch_id: str | None = None,
        include_inactive: bool = False,
    ) -> list[MemoryInfo]:
        normalized_ref = normalize_context_entity_ref(
            subject_ref,
            field="subject context subject_ref",
        )
        subject_kind = normalized_ref.split(":", 1)[0]
        kinds = SUBJECT_CONTEXT_READ_KINDS.get(subject_kind)
        if kinds is None:
            raise ValueError("subject decision context supports only actor:<id> or faction:<id>")
        return self.memory.list_for_subject_refs(
            campaign_id,
            subject_refs={normalized_ref},
            kinds=kinds,
            branch_id=branch_id,
            include_inactive=include_inactive,
        )
