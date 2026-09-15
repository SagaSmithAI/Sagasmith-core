"""Infrastructure-independent state change values."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CharacterStateUpdate:
    """A fully validated replacement for a character's JSON documents."""

    character_id: str
    sheet: dict[str, Any]
    notes: dict[str, Any]
    expected_revision: int | None = None
    name: str | None = None
    player_name: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class ActorKnowledgeTransfer:
    """Copy selected or complete current subjective knowledge to another actor."""

    source_actor_id: str
    destination_actor_id: str
    knowledge_key_prefix: str
    knowledge_ids: tuple[str, ...] = ()
    cause: str = "knowledge_transfer"
    disclosure_scope: str = "dm"
