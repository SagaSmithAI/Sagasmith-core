"""Atomic replacement of campaign state and character documents."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.characters import CharacterNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.idempotency import request_hash
from sagasmith_core.models import Campaign, Character, MutationGroup, RuleResolutionReceipt
from sagasmith_core.revisions import RevisionInfo, RevisionService


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
        expected_campaign_revision: int | None = None,
        operation: str | None = None,
        actor: str = "runtime",
        branch_id: str | None = None,
        idempotency_key: str | None = None,
        idempotency_request_hash: str | None = None,
        rule_receipts: list[dict[str, Any]] | None = None,
    ) -> list[RevisionInfo] | None:
        updates = list(character_updates or [])
        receipts = list(rule_receipts or [])
        ids = [item.character_id for item in updates]
        if len(ids) != len(set(ids)):
            raise ValueError("character updates must not contain duplicate ids")
        if campaign_state is None and not updates:
            raise ValueError("at least one state document must be supplied")
        if receipts and operation is None:
            raise ValueError("rule receipts require an audited operation")
        if idempotency_request_hash is not None:
            if not idempotency_key:
                raise ValueError("idempotency_request_hash requires an idempotency_key")
            if (
                len(idempotency_request_hash) != 64
                or any(character not in "0123456789abcdef" for character in idempotency_request_hash)
            ):
                raise ValueError("idempotency_request_hash must be a SHA-256 hex digest")

        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            effective_branch_id = branch_id or campaign.active_branch_id

            rows: list[tuple[Character, CharacterStateUpdate]] = []
            before_campaign = {
                "state": dict(campaign.state),
                "revision": campaign.revision,
            }
            if (
                expected_campaign_revision is not None
                and campaign.revision != expected_campaign_revision
            ):
                raise ValueError(
                    "campaign revision conflict: "
                    f"expected {expected_campaign_revision}, found {campaign.revision}"
                )
            if idempotency_key and session.scalar(
                select(MutationGroup.id).where(
                    MutationGroup.campaign_id == campaign_id,
                    MutationGroup.branch_id == effective_branch_id,
                    MutationGroup.idempotency_key == idempotency_key,
                    MutationGroup.applied.is_(True),
                )
            ):
                raise ValueError(
                    "idempotency key already has a committed mutation group; "
                    "read its replay receipt instead of applying another write"
                )
            before_characters: dict[str, dict[str, Any]] = {}
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
                before_characters[row.id] = {
                    "name": row.name,
                    "player_name": row.player_name,
                    "summary": row.summary,
                    "sheet": dict(row.sheet),
                    "notes": dict(row.notes),
                    "revision": row.revision,
                }
                rows.append((row, update))

            if campaign_state is not None:
                campaign.state = dict(campaign_state)
                campaign.revision += 1
            for row, update in rows:
                if update.name is not None:
                    row.name = update.name
                if update.player_name is not None:
                    row.player_name = update.player_name
                if update.summary is not None:
                    row.summary = update.summary
                row.sheet = dict(update.sheet)
                row.notes = dict(update.notes)
                row.revision += 1
            session.flush()
            if operation is None:
                return None
            changes: list[dict[str, Any]] = []
            if campaign_state is not None:
                changes.append(
                    {
                        "entity_type": "campaign",
                        "entity_id": campaign_id,
                        "before": before_campaign,
                        "after": {
                            "state": dict(campaign.state),
                            "revision": campaign.revision,
                        },
                    }
                )
            for row, _update in rows:
                changes.append(
                    {
                        "entity_type": "character",
                        "entity_id": row.id,
                        "before": before_characters[row.id],
                        "after": {
                            "name": row.name,
                            "player_name": row.player_name,
                            "summary": row.summary,
                            "sheet": dict(row.sheet),
                            "notes": dict(row.notes),
                            "revision": row.revision,
                        },
                    }
                )
            revisions = RevisionService(self.database).record_group_in_session(
                session,
                campaign_id,
                operation=operation,
                changes=changes,
                actor=actor,
                branch_id=branch_id,
                idempotency_key=idempotency_key,
                request_hash=idempotency_request_hash
                or request_hash(
                    {
                        "campaign_state": campaign_state,
                        "character_updates": [
                            {
                                "character_id": item.character_id,
                                "sheet": item.sheet,
                                "notes": item.notes,
                                "expected_revision": item.expected_revision,
                                "name": item.name,
                                "player_name": item.player_name,
                                "summary": item.summary,
                            }
                            for item in updates
                        ],
                    }
                )
                if idempotency_key
                else None,
            )
            mutation_group_id = revisions[0].mutation_group_id
            if receipts and mutation_group_id is None:
                raise RuntimeError("rule receipts require a mutation group")
            for receipt in receipts:
                fingerprint = str(receipt.get("ruleset_fingerprint") or "")
                mechanic_id = str(receipt.get("mechanic_id") or "")
                event = str(receipt.get("event") or "")
                if not fingerprint or not mechanic_id or not event:
                    raise ValueError(
                        "rule receipts require ruleset_fingerprint, mechanic_id, and event"
                    )
                session.add(
                    RuleResolutionReceipt(
                        id=str(uuid.uuid4()),
                        campaign_id=campaign_id,
                        branch_id=effective_branch_id,
                        mutation_group_id=mutation_group_id,
                        ruleset_fingerprint=fingerprint,
                        mechanic_id=mechanic_id,
                        event=event,
                        receipt=dict(receipt),
                    )
                )
            return revisions
