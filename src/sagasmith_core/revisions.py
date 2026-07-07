"""Audited state revisions with campaign-local undo and redo."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import (
    AuditLog,
    Campaign,
    Character,
    ItemInstance,
    MapScene,
    SceneRegion,
    SceneToken,
    StateRevision,
)


@dataclass(frozen=True)
class RevisionInfo:
    id: str
    campaign_id: str
    sequence: int
    branch_key: str
    operation: str
    entity_type: str
    entity_id: str
    applied: bool


class RevisionService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(
        self,
        campaign_id: str,
        *,
        operation: str,
        entity_type: str,
        entity_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        actor: str = "runtime",
    ) -> RevisionInfo:
        with self.database.transaction() as session:
            if session.get(Campaign, campaign_id) is None:
                raise CampaignNotFoundError(campaign_id)
            current = session.scalar(
                select(StateRevision)
                .where(
                    StateRevision.campaign_id == campaign_id,
                    StateRevision.applied.is_(True),
                )
                .order_by(StateRevision.sequence.desc())
            )
            sequence = (
                session.scalar(
                    select(func.max(StateRevision.sequence)).where(
                        StateRevision.campaign_id == campaign_id
                    )
                )
                or 0
            ) + 1
            branch_key = (
                current.branch_key
                if current is not None and not self._has_redo(session, campaign_id)
                else str(uuid.uuid4())
            )
            row = StateRevision(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                parent_id=current.id if current else None,
                sequence=sequence,
                branch_key=branch_key,
                operation=operation,
                entity_type=entity_type,
                entity_id=entity_id,
                before=before,
                after=after,
            )
            session.add(row)
            session.flush()
            self._audit(session, row, actor=actor)
            session.flush()
            return self._info(row)

    def undo(self, campaign_id: str) -> RevisionInfo:
        with self.database.transaction() as session:
            row = session.scalar(
                select(StateRevision)
                .where(
                    StateRevision.campaign_id == campaign_id,
                    StateRevision.applied.is_(True),
                )
                .order_by(StateRevision.sequence.desc())
            )
            if row is None:
                raise LookupError("nothing to undo")
            self._apply(session, row, row.before)
            row.applied = False
            self._audit(session, row, actor="undo", reverse=True)
            session.flush()
            return self._info(row)

    def redo(self, campaign_id: str) -> RevisionInfo:
        with self.database.transaction() as session:
            current = session.scalar(
                select(StateRevision)
                .where(
                    StateRevision.campaign_id == campaign_id,
                    StateRevision.applied.is_(True),
                )
                .order_by(StateRevision.sequence.desc())
            )
            statement = select(StateRevision).where(
                StateRevision.campaign_id == campaign_id,
                StateRevision.applied.is_(False),
            )
            if current:
                statement = statement.where(StateRevision.parent_id == current.id)
            else:
                statement = statement.where(StateRevision.parent_id.is_(None))
            row = session.scalar(statement.order_by(StateRevision.sequence))
            if row is None:
                raise LookupError("nothing to redo")
            self._apply(session, row, row.after)
            row.applied = True
            self._audit(session, row, actor="redo")
            session.flush()
            return self._info(row)

    def history(self, campaign_id: str, *, limit: int = 100) -> list[RevisionInfo]:
        with self.database.transaction() as session:
            rows = session.scalars(
                select(StateRevision)
                .where(StateRevision.campaign_id == campaign_id)
                .order_by(StateRevision.sequence.desc())
                .limit(max(1, min(limit, 500)))
            )
            return [self._info(row) for row in rows]

    @staticmethod
    def _has_redo(session, campaign_id: str) -> bool:
        return bool(
            session.scalar(
                select(func.count())
                .select_from(StateRevision)
                .where(
                    StateRevision.campaign_id == campaign_id,
                    StateRevision.applied.is_(False),
                )
            )
        )

    @staticmethod
    def _apply(session, revision: StateRevision, value: dict[str, Any] | None) -> None:
        if revision.entity_type == "campaign":
            row = session.get(Campaign, revision.entity_id)
        elif revision.entity_type == "character":
            row = session.get(Character, revision.entity_id)
        elif revision.entity_type == "item_instance":
            row = session.get(ItemInstance, revision.entity_id)
            if value is None:
                if row is not None:
                    session.delete(row)
                return
            if row is None:
                row = ItemInstance(**value)
                session.add(row)
                return
        elif revision.entity_type == "map_scene":
            row = session.get(MapScene, revision.entity_id)
            if value is None:
                if row is not None:
                    session.delete(row)
                return
            if row is None:
                row = MapScene(**value)
                session.add(row)
                return
        elif revision.entity_type == "scene_token":
            row = session.get(SceneToken, revision.entity_id)
            if value is None:
                if row is not None:
                    session.delete(row)
                return
            if row is None:
                row = SceneToken(**value)
                session.add(row)
                return
        elif revision.entity_type == "scene_region":
            row = session.get(SceneRegion, revision.entity_id)
            if value is None:
                if row is not None:
                    session.delete(row)
                return
            if row is None:
                row = SceneRegion(**value)
                session.add(row)
                return
        else:
            raise ValueError(f"unsupported reversible entity: {revision.entity_type}")
        if row is None:
            raise LookupError(revision.entity_id)
        for key, item in (value or {}).items():
            if key.startswith("_") or not hasattr(row, key):
                raise ValueError(f"unsupported reversible field: {key}")
            setattr(row, key, item)

    @staticmethod
    def _audit(session, row: StateRevision, *, actor: str, reverse: bool = False) -> None:
        session.add(
            AuditLog(
                id=str(uuid.uuid4()),
                campaign_id=row.campaign_id,
                revision_id=row.id,
                operation=f"{'reverse:' if reverse else ''}{row.operation}",
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                actor=actor,
                before=row.after if reverse else row.before,
                after=row.before if reverse else row.after,
            )
        )

    @staticmethod
    def _info(row: StateRevision) -> RevisionInfo:
        return RevisionInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            sequence=row.sequence,
            branch_key=row.branch_key,
            operation=row.operation,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            applied=row.applied,
        )
