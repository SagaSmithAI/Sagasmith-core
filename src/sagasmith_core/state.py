"""Atomic replacement of campaign state and character documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.characters import CharacterNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import Campaign, Character


@dataclass(frozen=True)
class CharacterStateUpdate:
    """A fully validated replacement for a character's JSON documents."""

    character_id: str
    sheet: dict[str, Any]
    notes: dict[str, Any]
    expected_revision: int | None = None


class StateMutationService:
    """Apply related campaign and character document changes atomically.

    Systems validate their own document schemas before calling this service.  Core
    only verifies campaign ownership and optimistic revisions, then commits all
    replacements together.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def replace(
        self,
        campaign_id: str,
        *,
        campaign_state: dict[str, Any] | None = None,
        character_updates: list[CharacterStateUpdate] | None = None,
    ) -> None:
        updates = list(character_updates or [])
        ids = [item.character_id for item in updates]
        if len(ids) != len(set(ids)):
            raise ValueError("character updates must not contain duplicate ids")
        if campaign_state is None and not updates:
            raise ValueError("at least one state document must be supplied")

        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)

            rows: list[tuple[Character, CharacterStateUpdate]] = []
            for update in updates:
                row = session.get(Character, update.character_id)
                if row is None:
                    raise CharacterNotFoundError(update.character_id)
                if row.campaign_id != campaign_id:
                    raise ValueError("character must belong to the target campaign")
                if (
                    update.expected_revision is not None
                    and row.revision != update.expected_revision
                ):
                    raise ValueError(f"character revision conflict: {update.character_id}")
                rows.append((row, update))

            if campaign_state is not None:
                campaign.state = dict(campaign_state)
                campaign.revision += 1
            for row, update in rows:
                row.sheet = dict(update.sheet)
                row.notes = dict(update.notes)
                row.revision += 1
            session.flush()
