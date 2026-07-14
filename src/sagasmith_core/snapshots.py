"""Immutable campaign snapshots with DAG lineage and integrity checks."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select, update

from sagasmith_core.branches import BranchService, resolve_branch
from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import (
    ActorKnowledge,
    ActorKnowledgeRevision,
    BranchActorKnowledgeHead,
    BranchFactHead,
    Campaign,
    CampaignBranch,
    CampaignEvent,
    CampaignMemory,
    CampaignRuleProfile,
    CampaignSnapshot,
    Character,
    MemoryRevision,
    MutationGroup,
    SceneProgress,
    SnapshotActorKnowledgeBinding,
    SnapshotEventBinding,
    SnapshotFactBinding,
    StateRevision,
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
    branch_id: str | None = None


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class SnapshotService:
    SCHEMA_VERSION = 2

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
            return self._create_in_session(
                session,
                campaign,
                label=label,
                recap=recap,
                parent_id=parent_id,
            )

    def _create_in_session(
        self,
        session,
        campaign: Campaign,
        *,
        label: str = "",
        recap: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> SnapshotInfo:
        branch = resolve_branch(session, campaign)
        parent_id = parent_id if parent_id is not None else branch.head_snapshot_id
        if parent_id:
            parent = session.get(CampaignSnapshot, parent_id)
            if parent is None or parent.campaign_id != campaign.id:
                raise LookupError(parent_id)
        slot = (
            session.scalar(
                select(func.max(CampaignSnapshot.slot)).where(
                    CampaignSnapshot.campaign_id == campaign.id
                )
            )
            or 0
        ) + 1
        payload = self._capture(session, campaign, branch.id)
        if recap is None:
            parent_payload = (
                dict(session.get(CampaignSnapshot, parent_id).payload) if parent_id else None
            )
            recap = self._build_recap(parent_payload, payload)
        session.execute(
            update(CampaignSnapshot)
            .where(
                CampaignSnapshot.campaign_id == campaign.id,
                CampaignSnapshot.branch_id == branch.id,
            )
            .values(is_head=False)
        )
        row = CampaignSnapshot(
            id=str(uuid.uuid4()),
            campaign_id=campaign.id,
            branch_id=branch.id,
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
        self._bind_continuity(session, row, branch)
        branch.head_snapshot_id = row.id
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
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            target = self._row(session, campaign_id, slot)
            if _checksum(target.payload) != target.checksum:
                raise SnapshotIntegrityError(f"snapshot {slot} failed checksum verification")
            if target.schema_version != self.SCHEMA_VERSION:
                raise SnapshotIntegrityError(
                    "snapshot schema is unsupported; create a new snapshot with the current runtime"
                )
            self._create_in_session(session, campaign, label=f"Before restore to slot {slot}")
            branch = CampaignBranch(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                name=f"restore-{slot}-{uuid.uuid4().hex[:8]}",
                base_snapshot_id=target.id,
                head_snapshot_id=target.id,
                is_current=False,
            )
            session.add(branch)
            session.flush()
            branch_service = BranchService(self.database)
            branch_service._copy_snapshot_heads(session, target.id, branch.id)
            branch_service._copy_snapshot_revisions(session, target.id, branch.id)
            branch_service._checkout(session, campaign, branch)
            self._apply(session, campaign, dict(target.payload))
            return self._create_in_session(
                session,
                campaign,
                label=f"Restored from slot {slot}",
                parent_id=target.id,
            )

    def checkout_branch(self, campaign_id: str, branch_id: str) -> SnapshotInfo | None:
        """Materialize a branch head without creating or deleting history."""
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch_row = session.get(CampaignBranch, branch_id)
            if branch_row is None or branch_row.campaign_id != campaign_id:
                raise LookupError(branch_id)
            BranchService._checkout(session, campaign, branch_row)
            if branch_row.head_snapshot_id is None:
                return None
            row = session.get(CampaignSnapshot, branch_row.head_snapshot_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(branch_row.head_snapshot_id)
            if _checksum(row.payload) != row.checksum:
                raise SnapshotIntegrityError("branch head failed checksum verification")
            self._apply(session, campaign, dict(row.payload))
            return self._info(row)

    def lineage(self, campaign_id: str, slot: int | None = None) -> list[SnapshotInfo]:
        with self.database.transaction() as session:
            if slot is None:
                campaign = session.get(Campaign, campaign_id)
                if campaign is None:
                    raise CampaignNotFoundError(campaign_id)
                row = session.get(
                    CampaignSnapshot, resolve_branch(session, campaign).head_snapshot_id
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
    def _capture(session, campaign: Campaign, branch_id: str) -> dict[str, Any]:
        profile = session.get(CampaignRuleProfile, campaign.id)
        characters = list(
            session.scalars(
                select(Character).where(Character.campaign_id == campaign.id).order_by(Character.id)
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
            .join(BranchFactHead, BranchFactHead.memory_id == CampaignMemory.id)
            .join(MemoryRevision, MemoryRevision.id == BranchFactHead.revision_id)
            .where(BranchFactHead.branch_id == branch_id)
            .order_by(CampaignMemory.id)
        )
        events = SnapshotService._visible_events(session, campaign.id, branch_id)
        knowledge_rows = session.execute(
            select(ActorKnowledge, ActorKnowledgeRevision)
            .join(
                BranchActorKnowledgeHead,
                BranchActorKnowledgeHead.knowledge_id == ActorKnowledge.id,
            )
            .join(
                ActorKnowledgeRevision,
                ActorKnowledgeRevision.id == BranchActorKnowledgeHead.revision_id,
            )
            .where(BranchActorKnowledgeHead.branch_id == branch_id)
            .order_by(ActorKnowledge.actor_id, ActorKnowledge.knowledge_key)
        )
        revisions = list(
            session.scalars(
                select(StateRevision)
                .join(MutationGroup, MutationGroup.id == StateRevision.mutation_group_id)
                .where(StateRevision.campaign_id == campaign.id)
                .where(MutationGroup.branch_id == branch_id)
                .order_by(StateRevision.sequence)
            )
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
                    "template_id": row.template_id,
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
            "events": [
                {
                    "id": row.id,
                    "sequence": row.sequence,
                    "event_type": row.event_type,
                    "summary": row.summary,
                    "payload": dict(row.payload),
                    "audience_scope": row.audience_scope,
                    "created_at": row.created_at.isoformat(),
                }
                for row in events
            ],
            "memories": [
                {
                    "id": memory.id,
                    "kind": memory.kind,
                    "subject": memory.subject,
                    "created_at": memory.created_at.isoformat(),
                    "updated_at": memory.updated_at.isoformat(),
                    "revision": {
                        "id": revision.id,
                        "snapshot_id": revision.snapshot_id,
                        "content": revision.content,
                        "metadata": dict(revision.metadata_json),
                        "created_at": revision.created_at.isoformat(),
                    },
                }
                for memory, revision in memory_rows
            ],
            "actor_knowledge": [
                {
                    "id": knowledge.id,
                    "actor_id": knowledge.actor_id,
                    "knowledge_key": knowledge.knowledge_key,
                    "subject_ref": knowledge.subject_ref,
                    "revision": {
                        "id": revision.id,
                        "proposition": revision.proposition,
                        "epistemic_status": revision.epistemic_status,
                        "confidence": revision.confidence,
                        "source_event_id": revision.source_event_id,
                        "cause": revision.cause,
                        "disclosure_scope": revision.disclosure_scope,
                    },
                }
                for knowledge, revision in knowledge_rows
            ],
            "revision_cursor": [
                {
                    "id": row.id,
                    "applied": row.applied,
                    "redoable": row.redoable,
                }
                for row in revisions
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
            item["name"] for key, item in new_characters.items() if old_characters.get(key) != item
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
        old_memories = {item["revision"]["id"] for item in previous.get("memories", [])}
        memory_candidates = [
            item["id"]
            for item in current.get("memories", [])
            if item["revision"]["id"] not in old_memories
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
        # Snapshot revisions describe the captured timeline, not the live
        # optimistic-concurrency token.  Never move the live token backwards
        # when switching to an older branch or restoring an earlier snapshot.
        campaign.revision = max(int(campaign.revision), int(value["revision"])) + 1

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

        session.execute(delete(SceneProgress).where(SceneProgress.campaign_id == campaign.id))
        for item in payload.get("scene_progress", []):
            item.setdefault("scope_id", "party")
            session.add(SceneProgress(campaign_id=campaign.id, **item))

        # Events, campaign facts, and actor knowledge are immutable ledgers.  Their
        # branch visibility is selected by bindings, so restore must never delete them.

        cursor = {item["id"]: item for item in payload.get("revision_cursor", [])}
        branch = resolve_branch(session, campaign)
        session.execute(
            update(StateRevision)
            .where(
                StateRevision.campaign_id == campaign.id,
                StateRevision.mutation_group_id.in_(
                    select(MutationGroup.id).where(MutationGroup.branch_id == branch.id)
                ),
            )
            .values(applied=False, redoable=False)
        )
        for revision_id, item in cursor.items():
            session.execute(
                update(StateRevision)
                .where(
                    StateRevision.campaign_id == campaign.id,
                    or_(StateRevision.id == revision_id, StateRevision.branch_key == revision_id),
                    StateRevision.mutation_group_id.in_(
                        select(MutationGroup.id).where(MutationGroup.branch_id == branch.id)
                    ),
                )
                .values(applied=item["applied"], redoable=item["redoable"])
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
            branch_id=row.branch_id,
        )

    @staticmethod
    def _visible_events(session, campaign_id: str, branch_id: str) -> list[CampaignEvent]:
        branch = session.get(CampaignBranch, branch_id)
        bound_ids: set[str] = set()
        if branch and branch.head_snapshot_id:
            bound_ids = set(
                session.scalars(
                    select(SnapshotEventBinding.event_id).where(
                        SnapshotEventBinding.snapshot_id == branch.head_snapshot_id
                    )
                )
            )
        rows = []
        if bound_ids:
            rows.extend(
                session.scalars(
                    select(CampaignEvent)
                    .where(CampaignEvent.id.in_(bound_ids))
                    .order_by(CampaignEvent.sequence)
                )
            )
        rows.extend(
            session.scalars(
                select(CampaignEvent)
                .where(
                    CampaignEvent.campaign_id == campaign_id,
                    CampaignEvent.branch_id == branch_id,
                    CampaignEvent.committed_snapshot_id.is_(None),
                )
                .order_by(CampaignEvent.sequence)
            )
        )
        return rows

    @classmethod
    def _bind_continuity(cls, session, snapshot: CampaignSnapshot, branch: CampaignBranch) -> None:
        for item in session.scalars(
            select(BranchFactHead).where(BranchFactHead.branch_id == branch.id)
        ):
            session.add(
                SnapshotFactBinding(
                    snapshot_id=snapshot.id,
                    memory_id=item.memory_id,
                    revision_id=item.revision_id,
                )
            )
        for item in session.scalars(
            select(BranchActorKnowledgeHead).where(BranchActorKnowledgeHead.branch_id == branch.id)
        ):
            session.add(
                SnapshotActorKnowledgeBinding(
                    snapshot_id=snapshot.id,
                    knowledge_id=item.knowledge_id,
                    revision_id=item.revision_id,
                )
            )
        for event in cls._visible_events(session, snapshot.campaign_id, branch.id):
            session.add(SnapshotEventBinding(snapshot_id=snapshot.id, event_id=event.id))
            if event.branch_id == branch.id and event.committed_snapshot_id is None:
                event.committed_snapshot_id = snapshot.id


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)
