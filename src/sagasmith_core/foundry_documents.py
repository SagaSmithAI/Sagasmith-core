"""Foundry-style runtime documents for non-UI tabletop execution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import (
    ActiveEffect,
    Campaign,
    GameActivity,
    GameActor,
    GameItem,
    GameMessage,
)


@dataclass(frozen=True)
class ActorDocument:
    id: str
    campaign_id: str
    system_id: str
    actor_type: str
    name: str
    img: str
    system: dict[str, Any]
    prototype_token: dict[str, Any]
    flags: dict[str, Any]
    derived: dict[str, Any]
    revision: int


@dataclass(frozen=True)
class ItemDocument:
    id: str
    campaign_id: str
    actor_id: str | None
    container_id: str | None
    system_id: str
    item_type: str
    name: str
    source_key: str
    img: str
    system: dict[str, Any]
    effects: list[dict[str, Any]]
    flags: dict[str, Any]
    sort: int
    revision: int


@dataclass(frozen=True)
class ActivityDocument:
    id: str
    campaign_id: str
    item_id: str
    activity_type: str
    name: str
    activation: dict[str, Any]
    consumption: dict[str, Any]
    duration: dict[str, Any]
    effects: list[dict[str, Any]]
    range: dict[str, Any]
    target: dict[str, Any]
    uses: dict[str, Any]
    system: dict[str, Any]
    flags: dict[str, Any]
    sort: int


@dataclass(frozen=True)
class EffectDocument:
    id: str
    campaign_id: str
    parent_type: str
    parent_id: str
    actor_id: str | None
    origin: str
    name: str
    img: str
    disabled: bool
    suppressed: bool
    transfer: bool
    duration: dict[str, Any]
    changes: list[dict[str, Any]]
    statuses: list[str]
    flags: dict[str, Any]


@dataclass(frozen=True)
class MessageDocument:
    id: str
    campaign_id: str
    sequence: int
    message_type: str
    speaker: dict[str, Any]
    actor_id: str | None
    item_id: str | None
    activity_id: str | None
    rolls: list[dict[str, Any]]
    deltas: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    narration_hints: list[str]
    content: str
    flags: dict[str, Any]
    created_at: str


class FoundryDocumentService:
    """Store Foundry-aligned documents without depending on Foundry UI code."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_actor(
        self,
        *,
        campaign_id: str,
        system_id: str,
        name: str,
        actor_type: str = "character",
        img: str = "",
        system: dict[str, Any] | None = None,
        prototype_token: dict[str, Any] | None = None,
        flags: dict[str, Any] | None = None,
        derived: dict[str, Any] | None = None,
    ) -> ActorDocument:
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            row = GameActor(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                system_id=system_id,
                actor_type=actor_type,
                name=name,
                img=img,
                system=dict(system or {}),
                prototype_token=dict(prototype_token or {}),
                flags=dict(flags or {}),
                derived=dict(derived or {}),
            )
            session.add(row)
            session.flush()
            return self._actor(row)

    def list_actors(
        self,
        campaign_id: str,
        *,
        actor_type: str | None = None,
    ) -> list[ActorDocument]:
        statement = select(GameActor).where(GameActor.campaign_id == campaign_id)
        if actor_type:
            statement = statement.where(GameActor.actor_type == actor_type)
        statement = statement.order_by(GameActor.name, GameActor.id)
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            return [self._actor(row) for row in session.scalars(statement)]

    def create_item(
        self,
        *,
        campaign_id: str,
        system_id: str,
        name: str,
        item_type: str = "loot",
        actor_id: str | None = None,
        container_id: str | None = None,
        source_key: str = "",
        img: str = "",
        system: dict[str, Any] | None = None,
        effects: list[dict[str, Any]] | None = None,
        flags: dict[str, Any] | None = None,
        sort: int = 0,
    ) -> ItemDocument:
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            if actor_id and session.get(GameActor, actor_id) is None:
                raise LookupError(actor_id)
            if container_id and session.get(GameItem, container_id) is None:
                raise LookupError(container_id)
            row = GameItem(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                actor_id=actor_id,
                container_id=container_id,
                system_id=system_id,
                item_type=item_type,
                name=name,
                source_key=source_key,
                img=img,
                system=dict(system or {}),
                effects=list(effects or []),
                flags=dict(flags or {}),
                sort=sort,
            )
            session.add(row)
            session.flush()
            return self._item(row)

    def list_items(
        self,
        campaign_id: str,
        *,
        actor_id: str | None = None,
        item_type: str | None = None,
    ) -> list[ItemDocument]:
        statement = select(GameItem).where(GameItem.campaign_id == campaign_id)
        if actor_id:
            statement = statement.where(GameItem.actor_id == actor_id)
        if item_type:
            statement = statement.where(GameItem.item_type == item_type)
        statement = statement.order_by(GameItem.sort, GameItem.name, GameItem.id)
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            return [self._item(row) for row in session.scalars(statement)]

    def create_activity(
        self,
        *,
        item_id: str,
        activity_type: str,
        name: str,
        activation: dict[str, Any] | None = None,
        consumption: dict[str, Any] | None = None,
        duration: dict[str, Any] | None = None,
        effects: list[dict[str, Any]] | None = None,
        range: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
        uses: dict[str, Any] | None = None,
        system: dict[str, Any] | None = None,
        flags: dict[str, Any] | None = None,
        sort: int = 0,
    ) -> ActivityDocument:
        with self.database.transaction() as session:
            item = session.get(GameItem, item_id)
            if item is None:
                raise LookupError(item_id)
            row = GameActivity(
                id=str(uuid.uuid4()),
                campaign_id=item.campaign_id,
                item_id=item_id,
                activity_type=activity_type,
                name=name,
                activation=dict(activation or {}),
                consumption=dict(consumption or {}),
                duration=dict(duration or {}),
                effects=list(effects or []),
                range=dict(range or {}),
                target=dict(target or {}),
                uses=dict(uses or {}),
                system=dict(system or {}),
                flags=dict(flags or {}),
                sort=sort,
            )
            session.add(row)
            session.flush()
            return self._activity(row)

    def list_activities(self, item_id: str) -> list[ActivityDocument]:
        statement = (
            select(GameActivity)
            .where(GameActivity.item_id == item_id)
            .order_by(GameActivity.sort, GameActivity.name, GameActivity.id)
        )
        with self.database.transaction() as session:
            if session.get(GameItem, item_id) is None:
                raise LookupError(item_id)
            return [self._activity(row) for row in session.scalars(statement)]

    def create_effect(
        self,
        *,
        campaign_id: str,
        parent_type: str,
        parent_id: str,
        name: str,
        actor_id: str | None = None,
        origin: str = "",
        img: str = "",
        disabled: bool = False,
        suppressed: bool = False,
        transfer: bool = False,
        duration: dict[str, Any] | None = None,
        changes: list[dict[str, Any]] | None = None,
        statuses: list[str] | None = None,
        flags: dict[str, Any] | None = None,
    ) -> EffectDocument:
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            if actor_id and session.get(GameActor, actor_id) is None:
                raise LookupError(actor_id)
            row = ActiveEffect(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                parent_type=parent_type,
                parent_id=parent_id,
                actor_id=actor_id,
                origin=origin,
                name=name,
                img=img,
                disabled=disabled,
                suppressed=suppressed,
                transfer=transfer,
                duration=dict(duration or {}),
                changes=list(changes or []),
                statuses=list(statuses or []),
                flags=dict(flags or {}),
            )
            session.add(row)
            session.flush()
            return self._effect(row)

    def list_effects(
        self,
        campaign_id: str,
        *,
        actor_id: str | None = None,
        parent_type: str | None = None,
        parent_id: str | None = None,
    ) -> list[EffectDocument]:
        statement = select(ActiveEffect).where(ActiveEffect.campaign_id == campaign_id)
        if actor_id:
            statement = statement.where(ActiveEffect.actor_id == actor_id)
        if parent_type:
            statement = statement.where(ActiveEffect.parent_type == parent_type)
        if parent_id:
            statement = statement.where(ActiveEffect.parent_id == parent_id)
        statement = statement.order_by(ActiveEffect.name, ActiveEffect.id)
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            return [self._effect(row) for row in session.scalars(statement)]

    def create_message(
        self,
        *,
        campaign_id: str,
        message_type: str,
        speaker: dict[str, Any] | None = None,
        actor_id: str | None = None,
        item_id: str | None = None,
        activity_id: str | None = None,
        rolls: list[dict[str, Any]] | None = None,
        deltas: list[dict[str, Any]] | None = None,
        pending: list[dict[str, Any]] | None = None,
        narration_hints: list[str] | None = None,
        content: str = "",
        flags: dict[str, Any] | None = None,
    ) -> MessageDocument:
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            sequence = (
                session.scalar(
                    select(func.max(GameMessage.sequence)).where(
                        GameMessage.campaign_id == campaign_id
                    )
                )
                or 0
            ) + 1
            row = GameMessage(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                sequence=sequence,
                message_type=message_type,
                speaker=dict(speaker or {}),
                actor_id=actor_id,
                item_id=item_id,
                activity_id=activity_id,
                rolls=list(rolls or []),
                deltas=list(deltas or []),
                pending=list(pending or []),
                narration_hints=list(narration_hints or []),
                content=content,
                flags=dict(flags or {}),
            )
            session.add(row)
            session.flush()
            return self._message(row)

    def list_messages(self, campaign_id: str, *, limit: int = 100) -> list[MessageDocument]:
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            rows = session.scalars(
                select(GameMessage)
                .where(GameMessage.campaign_id == campaign_id)
                .order_by(GameMessage.sequence.desc())
                .limit(max(1, min(limit, 500)))
            )
            return [self._message(row) for row in reversed(list(rows))]

    @staticmethod
    def _campaign(session, campaign_id: str) -> Campaign:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        return campaign

    @staticmethod
    def _actor(row: GameActor) -> ActorDocument:
        return ActorDocument(
            id=row.id,
            campaign_id=row.campaign_id,
            system_id=row.system_id,
            actor_type=row.actor_type,
            name=row.name,
            img=row.img,
            system=dict(row.system or {}),
            prototype_token=dict(row.prototype_token or {}),
            flags=dict(row.flags or {}),
            derived=dict(row.derived or {}),
            revision=row.revision,
        )

    @staticmethod
    def _item(row: GameItem) -> ItemDocument:
        return ItemDocument(
            id=row.id,
            campaign_id=row.campaign_id,
            actor_id=row.actor_id,
            container_id=row.container_id,
            system_id=row.system_id,
            item_type=row.item_type,
            name=row.name,
            source_key=row.source_key,
            img=row.img,
            system=dict(row.system or {}),
            effects=list(row.effects or []),
            flags=dict(row.flags or {}),
            sort=row.sort,
            revision=row.revision,
        )

    @staticmethod
    def _activity(row: GameActivity) -> ActivityDocument:
        return ActivityDocument(
            id=row.id,
            campaign_id=row.campaign_id,
            item_id=row.item_id,
            activity_type=row.activity_type,
            name=row.name,
            activation=dict(row.activation or {}),
            consumption=dict(row.consumption or {}),
            duration=dict(row.duration or {}),
            effects=list(row.effects or []),
            range=dict(row.range or {}),
            target=dict(row.target or {}),
            uses=dict(row.uses or {}),
            system=dict(row.system or {}),
            flags=dict(row.flags or {}),
            sort=row.sort,
        )

    @staticmethod
    def _effect(row: ActiveEffect) -> EffectDocument:
        return EffectDocument(
            id=row.id,
            campaign_id=row.campaign_id,
            parent_type=row.parent_type,
            parent_id=row.parent_id,
            actor_id=row.actor_id,
            origin=row.origin,
            name=row.name,
            img=row.img,
            disabled=row.disabled,
            suppressed=row.suppressed,
            transfer=row.transfer,
            duration=dict(row.duration or {}),
            changes=list(row.changes or []),
            statuses=list(row.statuses or []),
            flags=dict(row.flags or {}),
        )

    @staticmethod
    def _message(row: GameMessage) -> MessageDocument:
        return MessageDocument(
            id=row.id,
            campaign_id=row.campaign_id,
            sequence=row.sequence,
            message_type=row.message_type,
            speaker=dict(row.speaker or {}),
            actor_id=row.actor_id,
            item_id=row.item_id,
            activity_id=row.activity_id,
            rolls=list(row.rolls or []),
            deltas=list(row.deltas or []),
            pending=list(row.pending or []),
            narration_hints=list(row.narration_hints or []),
            content=row.content,
            flags=dict(row.flags or {}),
            created_at=row.created_at.isoformat(),
        )
