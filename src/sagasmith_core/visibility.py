"""Canonical visibility vocabularies for events, memory, and actor knowledge."""

from __future__ import annotations

EVENT_AUDIENCE_SCOPES = frozenset({"dm", "public", "party", "player", "actor"})
PLAYER_EVENT_AUDIENCE_SCOPES = frozenset({"public", "party", "player"})

MEMORY_DISCLOSURE_SCOPES = frozenset({"dm", "public", "party", "player"})
PLAYER_MEMORY_DISCLOSURE_SCOPES = frozenset({"public", "party", "player"})

ACTOR_KNOWLEDGE_DISCLOSURE_SCOPES = frozenset(
    {"dm", "owner", "party", "public", "player"}
)
PLAYER_OWNED_ACTOR_DISCLOSURE_SCOPES = frozenset(
    {"owner", "party", "public", "player"}
)
