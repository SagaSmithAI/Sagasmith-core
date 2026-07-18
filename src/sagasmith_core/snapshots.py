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
    CampaignRuleActivation,
    CampaignRuleProfile,
    CampaignSnapshot,
    Character,
    MemoryRevision,
    MutationGroup,
    RulePackVersion,
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
    SCHEMA_VERSION = 3

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
        if parent_id is not None and parent_id != branch.head_snapshot_id:
            raise ValueError("snapshot parent must be the checked-out branch head")
        parent_id = branch.head_snapshot_id
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
        session.flush()
        self._refresh_head_flags(session, campaign.id)
        return self._info(session, row)

    def regenerate_recap(self, campaign_id: str, slot: int) -> dict[str, Any]:
        """Rebuild a deterministic delta recap without changing snapshot state."""
        with self.database.transaction() as session:
            row = self._row(session, campaign_id, slot)
            parent = session.get(CampaignSnapshot, row.parent_id) if row.parent_id else None
            self._assert_integrity(session, row)
            if parent is not None:
                self._assert_integrity(session, parent)
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
            return [self._info(session, row) for row in rows]

    def get(self, campaign_id: str, slot: int) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = self._row(session, campaign_id, slot)
            return self._document(session, row)

    def get_by_id(self, campaign_id: str, snapshot_id: str) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = session.get(CampaignSnapshot, snapshot_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(snapshot_id)
            return self._document(session, row)

    def verify(self, campaign_id: str, slot: int) -> bool:
        with self.database.transaction() as session:
            return self._is_valid(session, self._row(session, campaign_id, slot))

    def assert_clean(self, campaign_id: str) -> None:
        """Reject branch switching when the checked-out worktree has not been saved."""
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign)
            self._assert_clean_branch(session, campaign, branch)

    def restore(self, campaign_id: str, slot: int) -> SnapshotInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            target = self._row(session, campaign_id, slot)
            self._assert_integrity(session, target)
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
            current = resolve_branch(session, campaign)
            if current.id == branch_row.id:
                if branch_row.head_snapshot_id is None:
                    return None
                row = session.get(CampaignSnapshot, branch_row.head_snapshot_id)
                if row is None:
                    raise LookupError(branch_row.head_snapshot_id)
                self._assert_integrity(session, row)
                return self._info(session, row)
            self._assert_clean_branch(session, campaign, current)
            if branch_row.head_snapshot_id is None:
                raise SnapshotIntegrityError("cannot checkout a branch without a snapshot head")
            row = session.get(CampaignSnapshot, branch_row.head_snapshot_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(branch_row.head_snapshot_id)
            self._assert_integrity(session, row)
            BranchService._checkout(session, campaign, branch_row)
            self._apply(session, campaign, dict(row.payload))
            return self._info(session, row)

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
            visited: set[str] = set()
            while row is not None:
                if row.id in visited:
                    raise SnapshotIntegrityError("snapshot lineage contains a cycle")
                if row.campaign_id != campaign_id:
                    raise SnapshotIntegrityError("snapshot lineage crosses campaign boundaries")
                visited.add(row.id)
                result.append(self._info(session, row))
                parent_id = row.parent_id
                row = session.get(CampaignSnapshot, parent_id) if parent_id else None
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
            references = session.scalar(
                select(func.count())
                .select_from(CampaignBranch)
                .where(
                    CampaignBranch.campaign_id == campaign_id,
                    or_(
                        CampaignBranch.head_snapshot_id == row.id,
                        CampaignBranch.base_snapshot_id == row.id,
                    ),
                )
            )
            if references:
                raise ValueError("cannot delete a snapshot referenced by a branch")
            session.delete(row)
            session.flush()
            self._refresh_head_flags(session, campaign_id)

    @staticmethod
    def _capture(session, campaign: Campaign, branch_id: str) -> dict[str, Any]:
        profile = session.get(CampaignRuleProfile, campaign.id)
        rule_lock = list(
            session.scalars(
                select(CampaignRuleActivation)
                .where(
                    CampaignRuleActivation.campaign_id == campaign.id,
                    CampaignRuleActivation.branch_id == branch_id,
                )
                .order_by(CampaignRuleActivation.pack_id)
            )
        )
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
            "rule_lock": [
                {
                    "pack_id": row.pack_id,
                    "version": row.version,
                    "checksum": row.checksum,
                    "enabled": row.enabled,
                    "options": dict(row.options),
                }
                for row in rule_lock
            ],
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
                    "current_location_key": row.current_location_key,
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
                "characters": [
                    item["name"]
                    for item in current.get("characters", [])
                    if item.get("character_type") == "pc"
                ],
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
            if item.get("character_type") == "pc" and old_characters.get(key) != item
        ]
        removed = [
            item["name"]
            for key, item in old_characters.items()
            if item.get("character_type") == "pc" and key not in new_characters
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
            and item["revision"].get("metadata", {}).get("disclosure_scope", "dm")
            in {"public", "party", "player"}
        ]
        old_event_ids = {item["id"] for item in previous.get("events", [])}
        event_changes = [
            item["summary"]
            for item in current.get("events", [])
            if item["id"] not in old_event_ids
            and item.get("audience_scope") in {"public", "party", "player"}
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
        if event_changes:
            summary_parts.append("recorded events")
        return {
            "summary": "; ".join(summary_parts) or "No material state changes",
            "plot_progress": scene_changes,
            "characters": character_changes,
            "removed_characters": removed,
            "locations": [],
            "events": event_changes,
            "future_impact": [],
            "player_choices": [],
            "memory_candidates": memory_candidates,
            "changed_fields": changed,
            "source": "deterministic",
        }

    @staticmethod
    def _apply(session, campaign: Campaign, payload: dict[str, Any]) -> None:
        rule_lock = list(payload.get("rule_lock") or [])
        for item in rule_lock:
            version = session.get(
                RulePackVersion,
                {"pack_id": item["pack_id"], "version": item["version"]},
            )
            if (
                version is None
                or version.status != "installed"
                or version.checksum != item["checksum"]
            ):
                raise SnapshotIntegrityError(
                    "snapshot rule lock is unavailable; install the exact pack version first"
                )
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

        branch = resolve_branch(session, campaign)
        session.execute(
            delete(CampaignRuleActivation).where(
                CampaignRuleActivation.campaign_id == campaign.id,
                CampaignRuleActivation.branch_id == branch.id,
            )
        )
        for item in rule_lock:
            session.add(
                CampaignRuleActivation(
                    campaign_id=campaign.id,
                    branch_id=branch.id,
                    pack_id=item["pack_id"],
                    version=item["version"],
                    checksum=item["checksum"],
                    enabled=bool(item.get("enabled", True)),
                    options=dict(item.get("options") or {}),
                )
            )
        session.flush()
        # Restoring a save must not make either its own branch or another
        # branch's lock incompatible with the campaign-global edition profile.
        from sagasmith_core.rule_packs import RulePackService

        RulePackService._assert_edition_compatible_in_session(
            session,
            campaign,
            profile.edition if profile is not None else "",
        )
        RulePackService._resolve(session, campaign, branch.id)

        session.execute(delete(Character).where(Character.campaign_id == campaign.id))
        for item in payload.get("characters", []):
            session.add(Character(campaign_id=campaign.id, **item))

        session.execute(delete(SceneProgress).where(SceneProgress.campaign_id == campaign.id))
        for item in payload.get("scene_progress", []):
            item.setdefault("scope_id", "party")
            item.setdefault("current_location_key", None)
            session.add(SceneProgress(campaign_id=campaign.id, **item))

        # Events, campaign facts, and actor knowledge are immutable ledgers.  Their
        # branch visibility is selected by bindings, so restore must never delete them.

        cursor = {item["id"]: item for item in payload.get("revision_cursor", [])}
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
    def _info(session, row: CampaignSnapshot) -> SnapshotInfo:
        is_head = bool(
            session.scalar(
                select(func.count())
                .select_from(CampaignBranch)
                .where(CampaignBranch.head_snapshot_id == row.id)
            )
        )
        return SnapshotInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            parent_id=row.parent_id,
            slot=row.slot,
            label=row.label,
            checksum=row.checksum,
            is_head=is_head,
            created_at=row.created_at.isoformat(),
            branch_id=row.branch_id,
        )

    @classmethod
    def _document(cls, session, row: CampaignSnapshot) -> dict[str, Any]:
        return {
            **asdict(cls._info(session, row)),
            "schema_version": row.schema_version,
            "payload": dict(row.payload),
            "recap": dict(row.recap) if row.recap else None,
            "storage_mode": "full",
            "valid": cls._is_valid(session, row),
        }

    @classmethod
    def _is_valid(cls, session, row: CampaignSnapshot) -> bool:
        try:
            cls._assert_integrity(session, row)
        except SnapshotIntegrityError:
            return False
        return True

    @classmethod
    def _assert_integrity(cls, session, row: CampaignSnapshot) -> None:
        """Verify the full payload, DAG ancestry, and indexed continuity bindings."""
        if row.schema_version != cls.SCHEMA_VERSION:
            raise SnapshotIntegrityError(
                "snapshot schema is unsupported; create a new snapshot with the current runtime"
            )
        if _checksum(row.payload) != row.checksum:
            raise SnapshotIntegrityError("snapshot payload failed checksum verification")
        payload = dict(row.payload)
        required = {
            "campaign",
            "rule_profile",
            "rule_lock",
            "characters",
            "scene_progress",
            "events",
            "memories",
            "actor_knowledge",
            "revision_cursor",
        }
        if not required.issubset(payload):
            raise SnapshotIntegrityError("snapshot payload is incomplete")

        visited = {row.id}
        parent_id = row.parent_id
        while parent_id:
            parent = session.get(CampaignSnapshot, parent_id)
            if parent is None or parent.campaign_id != row.campaign_id:
                raise SnapshotIntegrityError("snapshot lineage crosses campaign boundaries")
            if parent.id in visited:
                raise SnapshotIntegrityError("snapshot lineage contains a cycle")
            visited.add(parent.id)
            parent_id = parent.parent_id

        payload_facts = {str(item.get("id")): item for item in payload.get("memories", [])}
        expected_facts = cls._payload_revision_map(payload.get("memories", []), "memory")
        actual_facts = {
            item.memory_id: item.revision_id
            for item in session.scalars(
                select(SnapshotFactBinding).where(SnapshotFactBinding.snapshot_id == row.id)
            )
        }
        if expected_facts != actual_facts:
            raise SnapshotIntegrityError("snapshot fact bindings do not match its full payload")
        for memory_id, revision_id in actual_facts.items():
            memory = session.get(CampaignMemory, memory_id)
            revision = session.get(MemoryRevision, revision_id)
            item = payload_facts[memory_id]
            revision_item = dict(item.get("revision") or {})
            if (
                memory is None
                or revision is None
                or revision.memory_id != memory_id
                or memory.campaign_id != row.campaign_id
                or memory.kind != item.get("kind")
                or memory.subject != item.get("subject")
                or memory.created_at.isoformat() != item.get("created_at")
                or memory.updated_at.isoformat() != item.get("updated_at")
                or revision.snapshot_id != revision_item.get("snapshot_id")
                or revision.content != revision_item.get("content")
                or dict(revision.metadata_json) != dict(revision_item.get("metadata") or {})
                or revision.created_at.isoformat() != revision_item.get("created_at")
            ):
                raise SnapshotIntegrityError("snapshot fact binding targets the wrong revision")

        payload_knowledge = {
            str(item.get("id")): item for item in payload.get("actor_knowledge", [])
        }
        expected_knowledge = cls._payload_revision_map(
            payload.get("actor_knowledge", []), "actor knowledge"
        )
        actual_knowledge = {
            item.knowledge_id: item.revision_id
            for item in session.scalars(
                select(SnapshotActorKnowledgeBinding).where(
                    SnapshotActorKnowledgeBinding.snapshot_id == row.id
                )
            )
        }
        if expected_knowledge != actual_knowledge:
            raise SnapshotIntegrityError(
                "snapshot actor-knowledge bindings do not match its full payload"
            )
        actor_ids = {str(item.get("id")) for item in payload.get("characters", [])}
        for item in payload.get("actor_knowledge", []):
            if str(item.get("actor_id")) not in actor_ids:
                raise SnapshotIntegrityError("snapshot contains knowledge for a missing actor")
        for knowledge_id, revision_id in actual_knowledge.items():
            knowledge = session.get(ActorKnowledge, knowledge_id)
            revision = session.get(ActorKnowledgeRevision, revision_id)
            item = payload_knowledge[knowledge_id]
            revision_item = dict(item.get("revision") or {})
            if (
                knowledge is None
                or revision is None
                or revision.knowledge_id != knowledge_id
                or knowledge.campaign_id != row.campaign_id
                or knowledge.actor_id != item.get("actor_id")
                or knowledge.knowledge_key != item.get("knowledge_key")
                or knowledge.subject_ref != item.get("subject_ref")
                or revision.proposition != revision_item.get("proposition")
                or revision.epistemic_status != revision_item.get("epistemic_status")
                or revision.confidence != revision_item.get("confidence")
                or revision.source_event_id != revision_item.get("source_event_id")
                or revision.cause != revision_item.get("cause")
                or revision.disclosure_scope != revision_item.get("disclosure_scope")
            ):
                raise SnapshotIntegrityError(
                    "snapshot actor-knowledge binding targets the wrong revision"
                )

        payload_events = {str(item.get("id")): item for item in payload.get("events", [])}
        expected_events = [str(item.get("id")) for item in payload.get("events", [])]
        actual_events = list(
            session.scalars(
                select(SnapshotEventBinding.event_id).where(
                    SnapshotEventBinding.snapshot_id == row.id
                )
            )
        )
        if len(expected_events) != len(set(expected_events)) or set(expected_events) != set(
            actual_events
        ):
            raise SnapshotIntegrityError("snapshot event bindings do not match its full payload")
        for event_id in actual_events:
            event = session.get(CampaignEvent, event_id)
            item = payload_events[event_id]
            if (
                event is None
                or event.campaign_id != row.campaign_id
                or event.sequence != item.get("sequence")
                or event.event_type != item.get("event_type")
                or event.summary != item.get("summary")
                or dict(event.payload) != dict(item.get("payload") or {})
                or event.audience_scope != item.get("audience_scope")
                or event.created_at.isoformat() != item.get("created_at")
            ):
                raise SnapshotIntegrityError("snapshot event ledger differs from its full payload")

        for item in payload.get("revision_cursor", []):
            revision_id = str(item.get("id") or "")
            revision = session.get(StateRevision, revision_id) if revision_id else None
            if revision is None or revision.campaign_id != row.campaign_id:
                raise SnapshotIntegrityError("snapshot revision cursor is incomplete")

    @staticmethod
    def _payload_revision_map(items: list[dict[str, Any]], label: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in items:
            item_id = str(item.get("id") or "")
            revision = item.get("revision")
            revision_id = str(revision.get("id") or "") if isinstance(revision, dict) else ""
            if not item_id or not revision_id or item_id in result:
                raise SnapshotIntegrityError(f"snapshot {label} payload is malformed")
            result[item_id] = revision_id
        return result

    @staticmethod
    def _refresh_head_flags(session, campaign_id: str) -> None:
        """Keep the legacy flag aligned with the authoritative branch refs."""
        session.flush()
        head_ids = set(
            session.scalars(
                select(CampaignBranch.head_snapshot_id).where(
                    CampaignBranch.campaign_id == campaign_id,
                    CampaignBranch.head_snapshot_id.is_not(None),
                )
            )
        )
        session.execute(
            update(CampaignSnapshot)
            .where(CampaignSnapshot.campaign_id == campaign_id)
            .values(is_head=False)
        )
        if head_ids:
            session.execute(
                update(CampaignSnapshot)
                .where(CampaignSnapshot.id.in_(head_ids))
                .values(is_head=True)
            )

    @classmethod
    def _assert_clean_branch(cls, session, campaign: Campaign, branch: CampaignBranch) -> None:
        if branch.head_snapshot_id is None:
            raise ValueError("create a snapshot before switching branches")
        head = session.get(CampaignSnapshot, branch.head_snapshot_id)
        if head is None or head.campaign_id != campaign.id:
            raise SnapshotIntegrityError("checked-out branch head is missing")
        cls._assert_integrity(session, head)
        current = cls._capture(session, campaign, branch.id)
        expected = dict(head.payload)
        # Campaign revision is a live optimistic-concurrency token. Checkout
        # advances it monotonically, so it is not part of worktree dirtiness.
        current_campaign = dict(current.get("campaign") or {})
        expected_campaign = dict(expected.get("campaign") or {})
        current_campaign["revision"] = expected_campaign.get("revision")
        current["campaign"] = current_campaign
        if current != expected:
            raise ValueError(
                "checked-out branch has unsaved changes; create a snapshot before switching"
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
