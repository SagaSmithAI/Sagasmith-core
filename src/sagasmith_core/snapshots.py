"""Immutable campaign snapshots with DAG lineage and integrity checks."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import (
    Campaign,
    CampaignMemory,
    CampaignRuleProfile,
    CampaignSnapshot,
    Character,
    ActiveEffect,
    GameActivity,
    GameActor,
    GameItem,
    GameMessage,
    ItemInstance,
    MapScene,
    MemoryRevision,
    SceneRegion,
    SceneProgress,
    SceneToken,
)


class SnapshotIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotInfo:
    id: str
    campaign_id: str
    parent_id: str | None
    slot: int
    label: str
    checksum: str
    is_head: bool
    created_at: str


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class SnapshotService:
    SCHEMA_VERSION = 1

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        campaign_id: str,
        *,
        label: str = "",
        recap: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> SnapshotInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            head = session.scalar(
                select(CampaignSnapshot)
                .where(
                    CampaignSnapshot.campaign_id == campaign_id,
                    CampaignSnapshot.is_head.is_(True),
                )
                .order_by(CampaignSnapshot.slot.desc())
            )
            parent_id = parent_id if parent_id is not None else (head.id if head else None)
            if parent_id:
                parent = session.get(CampaignSnapshot, parent_id)
                if parent is None or parent.campaign_id != campaign_id:
                    raise LookupError(parent_id)
            slot = (
                session.scalar(
                    select(func.max(CampaignSnapshot.slot)).where(
                        CampaignSnapshot.campaign_id == campaign_id
                    )
                )
                or 0
            ) + 1
            payload = self._capture(session, campaign)
            if recap is None:
                parent_payload = (
                    dict(session.get(CampaignSnapshot, parent_id).payload)
                    if parent_id
                    else None
                )
                recap = self._build_recap(parent_payload, payload)
            session.execute(
                update(CampaignSnapshot)
                .where(CampaignSnapshot.campaign_id == campaign_id)
                .values(is_head=False)
            )
            row = CampaignSnapshot(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                parent_id=parent_id,
                slot=slot,
                label=label,
                schema_version=self.SCHEMA_VERSION,
                payload=payload,
                checksum=_checksum(payload),
                recap=recap,
                is_head=True,
            )
            session.add(row)
            session.flush()
            return self._info(row)

    def regenerate_recap(self, campaign_id: str, slot: int) -> dict[str, Any]:
        """Rebuild a deterministic delta recap without changing snapshot state."""
        with self.database.transaction() as session:
            row = self._row(session, campaign_id, slot)
            parent = session.get(CampaignSnapshot, row.parent_id) if row.parent_id else None
            row.recap = self._build_recap(
                dict(parent.payload) if parent else None,
                dict(row.payload),
            )
            session.flush()
            return dict(row.recap)

    def list(self, campaign_id: str) -> list[SnapshotInfo]:
        with self.database.transaction() as session:
            if session.get(Campaign, campaign_id) is None:
                raise CampaignNotFoundError(campaign_id)
            rows = session.scalars(
                select(CampaignSnapshot)
                .where(CampaignSnapshot.campaign_id == campaign_id)
                .order_by(CampaignSnapshot.slot)
            )
            return [self._info(row) for row in rows]

    def get(self, campaign_id: str, slot: int) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = self._row(session, campaign_id, slot)
            return {
                **asdict(self._info(row)),
                "schema_version": row.schema_version,
                "payload": dict(row.payload),
                "recap": dict(row.recap) if row.recap else None,
                "valid": _checksum(row.payload) == row.checksum,
            }

    def verify(self, campaign_id: str, slot: int) -> bool:
        return bool(self.get(campaign_id, slot)["valid"])

    def restore(self, campaign_id: str, slot: int) -> SnapshotInfo:
        target = self.get(campaign_id, slot)
        if not target["valid"]:
            raise SnapshotIntegrityError(f"snapshot {slot} failed checksum verification")
        self.create(campaign_id, label=f"Before restore to slot {slot}")
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            self._apply(session, campaign, target["payload"])
        return self.create(
            campaign_id,
            label=f"Restored from slot {slot}",
            parent_id=target["id"],
        )

    def lineage(self, campaign_id: str, slot: int | None = None) -> list[SnapshotInfo]:
        with self.database.transaction() as session:
            if slot is None:
                row = session.scalar(
                    select(CampaignSnapshot)
                    .where(
                        CampaignSnapshot.campaign_id == campaign_id,
                        CampaignSnapshot.is_head.is_(True),
                    )
                    .order_by(CampaignSnapshot.slot.desc())
                )
            else:
                row = self._row(session, campaign_id, slot)
            result: list[SnapshotInfo] = []
            while row is not None:
                result.append(self._info(row))
                row = session.get(CampaignSnapshot, row.parent_id) if row.parent_id else None
            return list(reversed(result))

    def export(self, campaign_id: str, slot: int, output: str | Path) -> dict[str, Any]:
        payload = self.get(campaign_id, slot)
        target = Path(output).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return payload

    def delete(self, campaign_id: str, slot: int) -> None:
        with self.database.transaction() as session:
            row = self._row(session, campaign_id, slot)
            children = session.scalar(
                select(func.count())
                .select_from(CampaignSnapshot)
                .where(CampaignSnapshot.parent_id == row.id)
            )
            if children:
                raise ValueError("cannot delete a snapshot that has descendants")
            was_head = row.is_head
            parent = session.get(CampaignSnapshot, row.parent_id) if row.parent_id else None
            session.delete(row)
            if was_head and parent is not None:
                parent.is_head = True

    @staticmethod
    def _capture(session, campaign: Campaign) -> dict[str, Any]:
        profile = session.get(CampaignRuleProfile, campaign.id)
        characters = list(
            session.scalars(
                select(Character)
                .where(Character.campaign_id == campaign.id)
                .order_by(Character.id)
            )
        )
        progress = list(
            session.scalars(
                select(SceneProgress)
                .where(SceneProgress.campaign_id == campaign.id)
                .order_by(SceneProgress.id)
            )
        )
        items = list(
            session.scalars(
                select(ItemInstance)
                .where(ItemInstance.campaign_id == campaign.id)
                .order_by(ItemInstance.id)
            )
        )
        map_scenes = list(
            session.scalars(
                select(MapScene)
                .where(MapScene.campaign_id == campaign.id)
                .order_by(MapScene.id)
            )
        )
        scene_tokens = list(
            session.scalars(
                select(SceneToken)
                .where(SceneToken.campaign_id == campaign.id)
                .order_by(SceneToken.id)
            )
        )
        scene_regions = list(
            session.scalars(
                select(SceneRegion)
                .where(SceneRegion.campaign_id == campaign.id)
                .order_by(SceneRegion.id)
            )
        )
        game_actors = list(
            session.scalars(
                select(GameActor)
                .where(GameActor.campaign_id == campaign.id)
                .order_by(GameActor.id)
            )
        )
        game_items = list(
            session.scalars(
                select(GameItem)
                .where(GameItem.campaign_id == campaign.id)
                .order_by(GameItem.id)
            )
        )
        game_activities = list(
            session.scalars(
                select(GameActivity)
                .where(GameActivity.campaign_id == campaign.id)
                .order_by(GameActivity.id)
            )
        )
        active_effects = list(
            session.scalars(
                select(ActiveEffect)
                .where(ActiveEffect.campaign_id == campaign.id)
                .order_by(ActiveEffect.id)
            )
        )
        game_messages = list(
            session.scalars(
                select(GameMessage)
                .where(GameMessage.campaign_id == campaign.id)
                .order_by(GameMessage.sequence, GameMessage.id)
            )
        )
        memory_rows = session.execute(
            select(CampaignMemory, MemoryRevision)
            .join(MemoryRevision, MemoryRevision.memory_id == CampaignMemory.id)
            .where(
                CampaignMemory.campaign_id == campaign.id,
                MemoryRevision.active.is_(True),
            )
            .order_by(CampaignMemory.id)
        )
        return {
            "campaign": {
                "name": campaign.name,
                "status": campaign.status,
                "description": campaign.description,
                "settings": dict(campaign.settings),
                "state": dict(campaign.state),
                "revision": campaign.revision,
            },
            "rule_profile": (
                {
                    "system_id": profile.system_id,
                    "edition": profile.edition,
                    "locale": profile.locale,
                    "publications": list(profile.publications),
                    "options": dict(profile.options),
                }
                if profile
                else None
            ),
            "characters": [
                {
                    "id": row.id,
                    "system_id": row.system_id,
                    "character_type": row.character_type,
                    "name": row.name,
                    "player_name": row.player_name,
                    "summary": row.summary,
                    "sheet": dict(row.sheet),
                    "notes": dict(row.notes),
                    "revision": row.revision,
                }
                for row in characters
            ],
            "scene_progress": [
                {
                    "id": row.id,
                    "scene_id": row.scene_id,
                    "scope_id": row.scope_id,
                    "status": row.status,
                    "progress": row.progress,
                    "current_room": row.current_room,
                    "state_version": row.state_version,
                    "state": dict(row.state),
                }
                for row in progress
            ],
            "items": [
                {
                    "id": row.id,
                    "template_id": row.template_id,
                    "name": row.name,
                    "owner_type": row.owner_type,
                    "owner_id": row.owner_id,
                    "container_id": row.container_id,
                    "quantity": row.quantity,
                    "equipped_slot": row.equipped_slot,
                    "attunement": row.attunement,
                    "identified": row.identified,
                    "charges": dict(row.charges),
                    "condition": row.condition,
                    "state": dict(row.state),
                }
                for row in items
            ],
            "map_scenes": [
                {
                    "id": row.id,
                    "name": row.name,
                    "grid_size": row.grid_size,
                    "grid_units": row.grid_units,
                    "width": row.width,
                    "height": row.height,
                    "background": row.background,
                    "active": row.active,
                    "metadata_json": dict(row.metadata_json),
                }
                for row in map_scenes
            ],
            "scene_tokens": [
                {
                    "id": row.id,
                    "scene_id": row.scene_id,
                    "actor_type": row.actor_type,
                    "actor_id": row.actor_id,
                    "name": row.name,
                    "x": row.x,
                    "y": row.y,
                    "width": row.width,
                    "height": row.height,
                    "elevation": row.elevation,
                    "disposition": row.disposition,
                    "hidden": row.hidden,
                    "vision": dict(row.vision),
                    "actor_delta": dict(row.actor_delta),
                    "metadata_json": dict(row.metadata_json),
                }
                for row in scene_tokens
            ],
            "scene_regions": [
                {
                    "id": row.id,
                    "scene_id": row.scene_id,
                    "name": row.name,
                    "shape": dict(row.shape),
                    "behavior": row.behavior,
                    "origin_activity_id": row.origin_activity_id,
                    "attached_token_id": row.attached_token_id,
                    "duration": dict(row.duration),
                    "metadata_json": dict(row.metadata_json),
                }
                for row in scene_regions
            ],
            "game_actors": [
                {
                    "id": row.id,
                    "system_id": row.system_id,
                    "actor_type": row.actor_type,
                    "name": row.name,
                    "img": row.img,
                    "system": dict(row.system),
                    "prototype_token": dict(row.prototype_token),
                    "flags": dict(row.flags),
                    "derived": dict(row.derived),
                    "revision": row.revision,
                }
                for row in game_actors
            ],
            "game_items": [
                {
                    "id": row.id,
                    "actor_id": row.actor_id,
                    "container_id": row.container_id,
                    "system_id": row.system_id,
                    "item_type": row.item_type,
                    "name": row.name,
                    "source_key": row.source_key,
                    "img": row.img,
                    "system": dict(row.system),
                    "effects": list(row.effects),
                    "flags": dict(row.flags),
                    "sort": row.sort,
                    "revision": row.revision,
                }
                for row in game_items
            ],
            "game_activities": [
                {
                    "id": row.id,
                    "item_id": row.item_id,
                    "activity_type": row.activity_type,
                    "name": row.name,
                    "activation": dict(row.activation),
                    "consumption": dict(row.consumption),
                    "duration": dict(row.duration),
                    "effects": list(row.effects),
                    "range": dict(row.range),
                    "target": dict(row.target),
                    "uses": dict(row.uses),
                    "system": dict(row.system),
                    "flags": dict(row.flags),
                    "sort": row.sort,
                }
                for row in game_activities
            ],
            "active_effects": [
                {
                    "id": row.id,
                    "parent_type": row.parent_type,
                    "parent_id": row.parent_id,
                    "actor_id": row.actor_id,
                    "origin": row.origin,
                    "name": row.name,
                    "img": row.img,
                    "disabled": row.disabled,
                    "suppressed": row.suppressed,
                    "transfer": row.transfer,
                    "duration": dict(row.duration),
                    "changes": list(row.changes),
                    "statuses": list(row.statuses),
                    "flags": dict(row.flags),
                }
                for row in active_effects
            ],
            "game_messages": [
                {
                    "id": row.id,
                    "sequence": row.sequence,
                    "message_type": row.message_type,
                    "speaker": dict(row.speaker),
                    "actor_id": row.actor_id,
                    "item_id": row.item_id,
                    "activity_id": row.activity_id,
                    "rolls": list(row.rolls),
                    "deltas": list(row.deltas),
                    "pending": list(row.pending),
                    "narration_hints": list(row.narration_hints),
                    "content": row.content,
                    "flags": dict(row.flags),
                }
                for row in game_messages
            ],
            "memories": [
                {
                    "memory_id": memory.id,
                    "revision_id": revision.id,
                }
                for memory, revision in memory_rows
            ],
        }

    @staticmethod
    def _build_recap(
        previous: dict[str, Any] | None,
        current: dict[str, Any],
    ) -> dict[str, Any]:
        """Describe state differences in a compact, model-independent shape."""
        if previous is None:
            return {
                "summary": "Campaign baseline",
                "plot_progress": [],
                "characters": [item["name"] for item in current.get("characters", [])],
                "locations": [],
                "events": [],
                "future_impact": [],
                "player_choices": [],
                "memory_candidates": [],
                "source": "deterministic",
            }
        changed: list[str] = []
        for field in ("status", "description", "settings", "state"):
            if previous.get("campaign", {}).get(field) != current.get("campaign", {}).get(field):
                changed.append(f"campaign.{field}")
        old_characters = {item["id"]: item for item in previous.get("characters", [])}
        new_characters = {item["id"]: item for item in current.get("characters", [])}
        character_changes = [
            item["name"]
            for key, item in new_characters.items()
            if old_characters.get(key) != item
        ]
        removed = [
            item["name"] for key, item in old_characters.items() if key not in new_characters
        ]
        old_scenes = {
            (item.get("scope_id", "party"), item["scene_id"]): item
            for item in previous.get("scene_progress", [])
        }
        scene_changes = [
            item["scene_id"]
            for item in current.get("scene_progress", [])
            if old_scenes.get((item.get("scope_id", "party"), item["scene_id"])) != item
        ]
        old_memories = {item["revision_id"] for item in previous.get("memories", [])}
        memory_candidates = [
            item["memory_id"]
            for item in current.get("memories", [])
            if item["revision_id"] not in old_memories
        ]
        old_items = {item["id"]: item for item in previous.get("items", [])}
        new_items = {item["id"]: item for item in current.get("items", [])}
        item_changes = [
            item["name"]
            for key, item in new_items.items()
            if old_items.get(key) != item
        ]
        removed_items = [
            item["name"] for key, item in old_items.items() if key not in new_items
        ]
        summary_parts = []
        if changed:
            summary_parts.append(f"updated {', '.join(changed)}")
        if character_changes or removed:
            summary_parts.append("changed characters")
        if scene_changes:
            summary_parts.append("advanced scenes")
        if memory_candidates:
            summary_parts.append("recorded memories")
        if item_changes or removed_items:
            summary_parts.append("changed inventory")
        if previous.get("map_scenes", []) != current.get("map_scenes", []):
            summary_parts.append("changed map scenes")
        if previous.get("scene_tokens", []) != current.get("scene_tokens", []):
            summary_parts.append("changed scene tokens")
        if previous.get("scene_regions", []) != current.get("scene_regions", []):
            summary_parts.append("changed scene regions")
        if previous.get("game_actors", []) != current.get("game_actors", []):
            summary_parts.append("changed game actors")
        if previous.get("game_items", []) != current.get("game_items", []):
            summary_parts.append("changed game items")
        if previous.get("game_activities", []) != current.get("game_activities", []):
            summary_parts.append("changed game activities")
        if previous.get("active_effects", []) != current.get("active_effects", []):
            summary_parts.append("changed active effects")
        if previous.get("game_messages", []) != current.get("game_messages", []):
            summary_parts.append("recorded game messages")
        return {
            "summary": "; ".join(summary_parts) or "No material state changes",
            "plot_progress": scene_changes,
            "characters": character_changes,
            "removed_characters": removed,
            "locations": [],
            "events": [],
            "future_impact": [],
            "player_choices": [],
            "memory_candidates": memory_candidates,
            "items": item_changes,
            "removed_items": removed_items,
            "changed_fields": changed,
            "source": "deterministic",
        }

    @staticmethod
    def _apply(session, campaign: Campaign, payload: dict[str, Any]) -> None:
        value = payload["campaign"]
        campaign.name = value["name"]
        campaign.status = value["status"]
        campaign.description = value["description"]
        campaign.settings = value["settings"]
        campaign.state = value["state"]
        campaign.revision = value["revision"]

        profile_value = payload.get("rule_profile")
        profile = session.get(CampaignRuleProfile, campaign.id)
        if profile_value is None and profile is not None:
            session.delete(profile)
        elif profile_value is not None:
            if profile is None:
                profile = CampaignRuleProfile(
                    campaign_id=campaign.id,
                    system_id=campaign.system_id,
                )
                session.add(profile)
            profile.edition = profile_value["edition"]
            profile.locale = profile_value["locale"]
            profile.publications = profile_value["publications"]
            profile.options = profile_value["options"]

        session.execute(delete(Character).where(Character.campaign_id == campaign.id))
        for item in payload.get("characters", []):
            session.add(Character(campaign_id=campaign.id, **item))

        session.execute(
            delete(SceneProgress).where(SceneProgress.campaign_id == campaign.id)
        )
        for item in payload.get("scene_progress", []):
            item.setdefault("scope_id", "party")
            session.add(SceneProgress(campaign_id=campaign.id, **item))

        session.execute(delete(ItemInstance).where(ItemInstance.campaign_id == campaign.id))
        for item in payload.get("items", []):
            session.add(ItemInstance(campaign_id=campaign.id, **item))

        session.execute(delete(SceneRegion).where(SceneRegion.campaign_id == campaign.id))
        session.execute(delete(SceneToken).where(SceneToken.campaign_id == campaign.id))
        session.execute(delete(MapScene).where(MapScene.campaign_id == campaign.id))
        for item in payload.get("map_scenes", []):
            session.add(MapScene(campaign_id=campaign.id, **item))
        session.flush()
        for item in payload.get("scene_tokens", []):
            session.add(SceneToken(campaign_id=campaign.id, **item))
        session.flush()
        for item in payload.get("scene_regions", []):
            session.add(SceneRegion(campaign_id=campaign.id, **item))

        session.execute(delete(GameMessage).where(GameMessage.campaign_id == campaign.id))
        session.execute(delete(ActiveEffect).where(ActiveEffect.campaign_id == campaign.id))
        session.execute(delete(GameActivity).where(GameActivity.campaign_id == campaign.id))
        session.execute(delete(GameItem).where(GameItem.campaign_id == campaign.id))
        session.execute(delete(GameActor).where(GameActor.campaign_id == campaign.id))
        for item in payload.get("game_actors", []):
            session.add(GameActor(campaign_id=campaign.id, **item))
        session.flush()
        for item in payload.get("game_items", []):
            session.add(GameItem(campaign_id=campaign.id, **item))
        session.flush()
        for item in payload.get("game_activities", []):
            session.add(GameActivity(campaign_id=campaign.id, **item))
        session.flush()
        for item in payload.get("active_effects", []):
            session.add(ActiveEffect(campaign_id=campaign.id, **item))
        session.flush()
        for item in payload.get("game_messages", []):
            session.add(GameMessage(campaign_id=campaign.id, **item))

        active_ids = {item["revision_id"] for item in payload.get("memories", [])}
        memory_ids = select(CampaignMemory.id).where(
            CampaignMemory.campaign_id == campaign.id
        )
        session.execute(
            update(MemoryRevision)
            .where(MemoryRevision.memory_id.in_(memory_ids))
            .values(active=False)
        )
        if active_ids:
            session.execute(
                update(MemoryRevision)
                .where(MemoryRevision.id.in_(active_ids))
                .values(active=True)
            )

    @staticmethod
    def _row(session, campaign_id: str, slot: int) -> CampaignSnapshot:
        row = session.scalar(
            select(CampaignSnapshot).where(
                CampaignSnapshot.campaign_id == campaign_id,
                CampaignSnapshot.slot == slot,
            )
        )
        if row is None:
            raise LookupError(f"snapshot slot {slot}")
        return row

    @staticmethod
    def _info(row: CampaignSnapshot) -> SnapshotInfo:
        return SnapshotInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            parent_id=row.parent_id,
            slot=row.slot,
            label=row.label,
            checksum=row.checksum,
            is_head=row.is_head,
            created_at=row.created_at.isoformat(),
        )
