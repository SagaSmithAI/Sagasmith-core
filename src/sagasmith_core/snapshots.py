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
    MemoryRevision,
    SceneProgress,
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
                    "status": row.status,
                    "progress": row.progress,
                    "current_room": row.current_room,
                    "state_version": row.state_version,
                    "state": dict(row.state),
                }
                for row in progress
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
            session.add(SceneProgress(campaign_id=campaign.id, **item))

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
