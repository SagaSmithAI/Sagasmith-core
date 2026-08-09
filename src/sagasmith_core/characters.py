"""Character library and campaign binding service."""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.content_pack import ACTOR_CARD_SCHEMA
from sagasmith_core.database import Database
from sagasmith_core.idempotency import IdempotencyService
from sagasmith_core.models import Campaign, Character
from sagasmith_core.portable import build_actor_card, validate_actor_card


class CharacterNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class CharacterInfo:
    id: str
    system_id: str
    campaign_id: str | None
    template_id: str | None
    character_type: str
    name: str
    player_name: str | None
    summary: str
    sheet: dict[str, Any]
    notes: dict[str, Any]
    revision: int


class CharacterService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        system_id: str,
        name: str,
        character_type: str = "pc",
        campaign_id: str | None = None,
        player_name: str | None = None,
        summary: str = "",
        sheet: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
    ) -> CharacterInfo:
        with self.database.transaction() as session:
            self._validate_campaign(session, system_id, campaign_id)
            row = Character(
                id=str(uuid.uuid4()),
                system_id=system_id,
                campaign_id=campaign_id,
                character_type=character_type,
                name=name,
                player_name=player_name,
                summary=summary,
                sheet=sheet or {},
                notes=notes or {},
            )
            session.add(row)
            session.flush()
            return self._info(row)

    def get(self, character_id: str) -> CharacterInfo:
        with self.database.transaction() as session:
            row = session.get(Character, character_id)
            if row is None:
                raise CharacterNotFoundError(character_id)
            return self._info(row)

    def export_portable_card(
        self,
        character_id: str,
        *,
        portable_id: str,
        version: str = "1.0.0",
        metadata: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
        bindings: list[dict[str, Any]] | None = None,
        image: dict[str, Any] | None = None,
        dependencies: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Export an actor without leaking campaign identity or runtime revision."""

        character = self.get(character_id)
        return build_actor_card(
            portable_id=portable_id,
            version=version,
            system_id=character.system_id,
            actor_type=character.character_type,
            name=character.name,
            player_name=character.player_name,
            summary=character.summary,
            sheet=character.sheet,
            notes=character.notes,
            provenance=provenance,
            bindings=bindings,
            image=image,
            metadata=metadata,
            dependencies=dependencies,
        )

    def import_portable_card(
        self,
        card: dict[str, Any],
        *,
        campaign_id: str | None = None,
        player_name: str | None = None,
        name: str | None = None,
        principal_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CharacterInfo:
        """Create a fresh local actor from a validated portable card.

        System-specific callers must validate the sheet and notes before this
        boundary.  Import never transfers actor knowledge, campaign state, a
        source database id, or a source revision.
        """

        value = validate_actor_card(card)
        payload = value["payload"]
        arguments = {
            "system_id": value["system_id"],
            "name": name if name is not None else payload["name"],
            "character_type": payload["actor_type"],
            "campaign_id": campaign_id,
            "player_name": (player_name if player_name is not None else payload["player_name"]),
            "summary": payload["summary"],
            "sheet": copy.deepcopy(payload["sheet"]),
            "notes": copy.deepcopy(payload["notes"]),
        }
        if principal_id is None and idempotency_key is None:
            return self.create(**arguments)
        if principal_id is None or idempotency_key is None:
            raise ValueError("principal_id and idempotency_key must be supplied together")
        return self.create_idempotent(
            **arguments,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
        )

    def import_content_actor(
        self,
        actor: dict[str, Any],
        *,
        campaign_id: str | None = None,
        player_name: str | None = None,
        name: str | None = None,
        principal_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CharacterInfo:
        """Create a fresh runtime actor from a v3 package-owned actor card."""

        if actor.get("schema") != ACTOR_CARD_SCHEMA:
            raise ValueError(f"actor.schema must be {ACTOR_CARD_SCHEMA!r}")
        arguments = {
            "system_id": str(actor["system_id"]),
            "name": name if name is not None else str(actor["name"]),
            "character_type": str(actor["actor_type"]),
            "campaign_id": campaign_id,
            "player_name": (player_name if player_name is not None else actor.get("player_name")),
            "summary": str(actor.get("summary") or ""),
            "sheet": copy.deepcopy(dict(actor["sheet"])),
            "notes": copy.deepcopy(dict(actor["notes"])),
        }
        if principal_id is None and idempotency_key is None:
            return self.create(**arguments)
        if principal_id is None or idempotency_key is None:
            raise ValueError("principal_id and idempotency_key must be supplied together")
        return self.create_idempotent(
            **arguments,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
        )

    def list(
        self,
        *,
        system_id: str | None = None,
        campaign_id: str | None = None,
        character_type: str | None = None,
    ) -> list[CharacterInfo]:
        statement = select(Character).order_by(Character.name, Character.id)
        if system_id:
            statement = statement.where(Character.system_id == system_id)
        if campaign_id:
            statement = statement.where(Character.campaign_id == campaign_id)
        if character_type:
            statement = statement.where(Character.character_type == character_type)
        with self.database.transaction() as session:
            return [self._info(row) for row in session.scalars(statement)]

    def list_library(
        self,
        *,
        system_id: str | None = None,
        character_type: str | None = None,
    ) -> list[CharacterInfo]:
        statement = (
            select(Character)
            .where(Character.campaign_id.is_(None))
            .order_by(Character.name, Character.id)
        )
        if system_id:
            statement = statement.where(Character.system_id == system_id)
        if character_type:
            statement = statement.where(Character.character_type == character_type)
        with self.database.transaction() as session:
            return [self._info(row) for row in session.scalars(statement)]

    def instantiate(
        self,
        template_id: str,
        *,
        campaign_id: str,
        name: str | None = None,
        player_name: str | None = None,
        sheet: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
    ) -> CharacterInfo:
        """Copy a library character into a campaign as an independent instance."""
        with self.database.transaction() as session:
            template = session.get(Character, template_id)
            if template is None:
                raise CharacterNotFoundError(template_id)
            if template.campaign_id is not None:
                raise ValueError("only a library character can be instantiated")
            self._validate_campaign(session, template.system_id, campaign_id)
            row = Character(
                id=str(uuid.uuid4()),
                system_id=template.system_id,
                campaign_id=campaign_id,
                template_id=template.id,
                character_type=template.character_type,
                name=name if name is not None else template.name,
                player_name=(player_name if player_name is not None else template.player_name),
                summary=template.summary,
                sheet=copy.deepcopy(template.sheet if sheet is None else sheet),
                notes=copy.deepcopy(template.notes if notes is None else notes),
            )
            session.add(row)
            session.flush()
            return self._info(row)

    def create_idempotent(
        self,
        *,
        system_id: str,
        name: str,
        principal_id: str,
        idempotency_key: str,
        character_type: str = "pc",
        campaign_id: str | None = None,
        player_name: str | None = None,
        summary: str = "",
        sheet: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
    ) -> CharacterInfo:
        """Create one character and its replay receipt in the same transaction."""
        payload = {
            "system_id": system_id,
            "name": name,
            "character_type": character_type,
            "campaign_id": campaign_id,
            "player_name": player_name,
            "summary": summary,
            "sheet": sheet or {},
            "notes": notes or {},
        }
        scope = f"character-create:{campaign_id or 'library'}:{principal_id}"
        idempotency = IdempotencyService(self.database)
        with self.database.transaction() as session:
            replay = idempotency.lookup_in_session(session, scope, idempotency_key, payload)
            if replay is not None and replay.response is not None:
                return CharacterInfo(**replay.response)
            self._validate_campaign(session, system_id, campaign_id)
            row = Character(
                id=str(uuid.uuid4()),
                system_id=system_id,
                campaign_id=campaign_id,
                character_type=character_type,
                name=name,
                player_name=player_name,
                summary=summary,
                sheet=sheet or {},
                notes=notes or {},
            )
            session.add(row)
            session.flush()
            result = self._info(row)
            idempotency.remember_in_session(
                session,
                scope,
                idempotency_key,
                payload,
                result.__dict__,
                campaign_id=campaign_id,
            )
            return result

    def create_with_instance(
        self,
        *,
        system_id: str,
        campaign_id: str,
        name: str,
        character_type: str = "pc",
        player_name: str | None = None,
        summary: str = "",
        sheet: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
        principal_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[CharacterInfo, CharacterInfo]:
        """Create a public-library template and an independent campaign instance."""
        if (principal_id is None) != (idempotency_key is None):
            raise ValueError("principal_id and idempotency_key must be supplied together")
        payload = {
            "system_id": system_id,
            "campaign_id": campaign_id,
            "name": name,
            "character_type": character_type,
            "player_name": player_name,
            "summary": summary,
            "sheet": sheet or {},
            "notes": notes or {},
        }
        scope = f"character-build:{campaign_id}:{principal_id}" if principal_id else None
        idempotency = IdempotencyService(self.database)
        with self.database.transaction() as session:
            if scope is not None and idempotency_key is not None:
                replay = idempotency.lookup_in_session(session, scope, idempotency_key, payload)
                if replay is not None and replay.response is not None:
                    return (
                        CharacterInfo(**replay.response["template"]),
                        CharacterInfo(**replay.response["instance"]),
                    )
            self._validate_campaign(session, system_id, campaign_id)
            template = Character(
                id=str(uuid.uuid4()),
                system_id=system_id,
                character_type=character_type,
                name=name,
                summary=summary,
                sheet=copy.deepcopy(sheet or {}),
                notes=copy.deepcopy(notes or {}),
            )
            instance = Character(
                id=str(uuid.uuid4()),
                system_id=system_id,
                campaign_id=campaign_id,
                template_id=template.id,
                character_type=character_type,
                name=name,
                player_name=player_name,
                summary=summary,
                sheet=copy.deepcopy(sheet or {}),
                notes=copy.deepcopy(notes or {}),
            )
            session.add_all([template, instance])
            session.flush()
            result = self._info(template), self._info(instance)
            if scope is not None and idempotency_key is not None:
                idempotency.remember_in_session(
                    session,
                    scope,
                    idempotency_key,
                    payload,
                    {
                        "template": result[0].__dict__,
                        "instance": result[1].__dict__,
                    },
                    campaign_id=campaign_id,
                )
            return result

    def update(
        self,
        character_id: str,
        *,
        name: str | None = None,
        player_name: str | None = None,
        summary: str | None = None,
        sheet: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> CharacterInfo:
        with self.database.transaction() as session:
            row = session.get(Character, character_id)
            if row is None:
                raise CharacterNotFoundError(character_id)
            if expected_revision is not None and row.revision != expected_revision:
                raise ValueError(f"character revision conflict: {character_id}")
            if name is not None:
                row.name = name
            if player_name is not None:
                row.player_name = player_name
            if summary is not None:
                row.summary = summary
            if sheet is not None:
                row.sheet = sheet
            if notes is not None:
                row.notes = notes
            row.revision += 1
            session.flush()
            return self._info(row)

    def bind(self, character_id: str, campaign_id: str | None) -> CharacterInfo:
        with self.database.transaction() as session:
            row = session.get(Character, character_id)
            if row is None:
                raise CharacterNotFoundError(character_id)
            self._validate_campaign(session, row.system_id, campaign_id)
            row.campaign_id = campaign_id
            row.revision += 1
            session.flush()
            return self._info(row)

    @staticmethod
    def _validate_campaign(session, system_id: str, campaign_id: str | None) -> None:
        if campaign_id is None:
            return
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        if campaign.system_id != system_id:
            raise ValueError("character and campaign must use the same system_id")

    @staticmethod
    def _info(row: Character) -> CharacterInfo:
        return CharacterInfo(
            id=row.id,
            system_id=row.system_id,
            campaign_id=row.campaign_id,
            template_id=row.template_id,
            character_type=row.character_type,
            name=row.name,
            player_name=row.player_name,
            summary=row.summary,
            sheet=dict(row.sheet),
            notes=dict(row.notes),
            revision=row.revision,
        )
