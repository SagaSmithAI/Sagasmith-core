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
    MutationGroup,
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
    redoable: bool
    mutation_group_id: str | None = None


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
        return self.record_group(
            campaign_id,
            operation=operation,
            changes=[
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "before": before,
                    "after": after,
                }
            ],
            actor=actor,
        )[0]

    def record_group(
        self,
        campaign_id: str,
        *,
        operation: str,
        changes: list[dict[str, Any]],
        actor: str = "runtime",
        branch_id: str | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> list[RevisionInfo]:
        """Record one user-visible mutation touching one or many entities."""
        with self.database.transaction() as session:
            return self.record_group_in_session(
                session,
                campaign_id,
                operation=operation,
                changes=changes,
                actor=actor,
                branch_id=branch_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )

    def record_group_in_session(
        self,
        session,
        campaign_id: str,
        *,
        operation: str,
        changes: list[dict[str, Any]],
        actor: str = "runtime",
        branch_id: str | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> list[RevisionInfo]:
        """Record a group inside an existing state transaction."""
        if not changes:
            raise ValueError("mutation group must contain at least one change")
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        effective_branch_id = branch_id or campaign.active_branch_id
        current = session.scalar(
            select(StateRevision)
            .join(MutationGroup, MutationGroup.id == StateRevision.mutation_group_id)
            .where(
                StateRevision.campaign_id == campaign_id,
                StateRevision.applied.is_(True),
                MutationGroup.branch_id == effective_branch_id,
            )
            .order_by(StateRevision.sequence.desc())
            .limit(1)
        )
        max_sequence = (
            session.scalar(
                select(func.max(StateRevision.sequence)).where(
                    StateRevision.campaign_id == campaign_id
                )
            )
            or 0
        )
        group = MutationGroup(
            id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            branch_id=effective_branch_id,
            sequence=max_sequence + 1,
            operation=operation,
            actor=actor,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        session.add(group)
        if self._has_redo(session, campaign_id, effective_branch_id):
            session.query(MutationGroup).filter(
                MutationGroup.campaign_id == campaign_id,
                MutationGroup.branch_id == effective_branch_id,
                MutationGroup.applied.is_(False),
                MutationGroup.redoable.is_(True),
            ).update({MutationGroup.redoable: False}, synchronize_session=False)
            session.query(StateRevision).filter(
                StateRevision.campaign_id == campaign_id,
                StateRevision.mutation_group_id.in_(
                    select(MutationGroup.id).where(MutationGroup.branch_id == effective_branch_id)
                ),
                StateRevision.applied.is_(False),
                StateRevision.redoable.is_(True),
            ).update({StateRevision.redoable: False}, synchronize_session=False)
        branch_key = (
            current.branch_key
            if current is not None and not self._has_redo(session, campaign_id, effective_branch_id)
            else str(uuid.uuid4())
        )
        rows: list[StateRevision] = []
        parent_id = current.id if current else None
        for offset, change in enumerate(changes):
            row = StateRevision(
                id=str(uuid.uuid4()),
                mutation_group_id=group.id,
                campaign_id=campaign_id,
                parent_id=parent_id,
                sequence=max_sequence + offset + 1,
                branch_key=branch_key,
                operation=operation,
                entity_type=str(change["entity_type"]),
                entity_id=str(change["entity_id"]),
                before=change.get("before"),
                after=change.get("after"),
            )
            session.add(row)
            session.flush()
            self._audit(session, row, actor=actor)
            rows.append(row)
            parent_id = row.id
        session.flush()
        return [self._info(row) for row in rows]

    def undo(self, campaign_id: str) -> RevisionInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            row = session.scalar(
                select(StateRevision)
                .join(MutationGroup, MutationGroup.id == StateRevision.mutation_group_id)
                .where(
                    StateRevision.campaign_id == campaign_id,
                    StateRevision.applied.is_(True),
                    self._visible_branch_revision_clause(session, campaign),
                )
                .order_by(StateRevision.sequence.desc())
                .limit(1)
            )
            if row is None:
                raise LookupError("nothing to undo")
            rows = self._group_rows(session, row)
            for member in sorted(rows, key=lambda item: item.sequence, reverse=True):
                self._apply(session, member, member.before)
                member.applied = False
                self._audit(session, member, actor="undo", reverse=True)
            if row.mutation_group_id:
                group = session.get(MutationGroup, row.mutation_group_id)
                if group is not None:
                    group.applied = False
            session.flush()
            return self._info(row)

    def redo(self, campaign_id: str) -> RevisionInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch_id = campaign.active_branch_id
            current = session.scalar(
                select(StateRevision)
                .join(MutationGroup, MutationGroup.id == StateRevision.mutation_group_id)
                .where(
                    StateRevision.campaign_id == campaign_id,
                    StateRevision.applied.is_(True),
                    self._visible_branch_revision_clause(session, campaign),
                )
                .order_by(StateRevision.sequence.desc())
                .limit(1)
            )
            current_group_id = current.mutation_group_id if current else None
            statement = select(MutationGroup).where(
                MutationGroup.campaign_id == campaign_id,
                MutationGroup.branch_id == branch_id,
                MutationGroup.applied.is_(False),
                MutationGroup.redoable.is_(True),
            )
            if current_group_id:
                current_sequence = (
                    select(MutationGroup.sequence)
                    .where(MutationGroup.id == current_group_id)
                    .scalar_subquery()
                )
                statement = statement.where(MutationGroup.sequence > current_sequence)
            group = session.scalar(statement.order_by(MutationGroup.sequence).limit(1))
            if group is None:
                raise LookupError("nothing to redo")
            rows = self._group_rows(session, group_id=group.id)
            for member in sorted(rows, key=lambda item: item.sequence):
                self._apply(session, member, member.after)
                member.applied = True
                self._audit(session, member, actor="redo")
            group.applied = True
            row = rows[-1]
            session.flush()
            return self._info(row)

    def history(self, campaign_id: str, *, limit: int = 100) -> list[RevisionInfo]:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            rows = session.scalars(
                select(StateRevision)
                .join(MutationGroup, MutationGroup.id == StateRevision.mutation_group_id)
                .where(
                    StateRevision.campaign_id == campaign_id,
                    self._visible_branch_revision_clause(session, campaign),
                )
                .order_by(StateRevision.sequence.desc())
                .limit(max(1, min(limit, 500)))
            )
            return [self._info(row) for row in rows]

    @staticmethod
    def _has_redo(session, campaign_id: str, branch_id: str | None) -> bool:
        return bool(
            session.scalar(
                select(func.count())
                .select_from(MutationGroup)
                .where(
                    MutationGroup.campaign_id == campaign_id,
                    MutationGroup.branch_id == branch_id,
                    MutationGroup.applied.is_(False),
                    MutationGroup.redoable.is_(True),
                )
            )
        )

    @staticmethod
    def _visible_branch_revision_clause(session, campaign: Campaign):
        """Revision rows are branch-owned; fork setup clones a snapshot cursor."""
        return MutationGroup.branch_id == campaign.active_branch_id

    @staticmethod
    def _group_rows(
        session,
        row: StateRevision | None = None,
        *,
        group_id: str | None = None,
    ) -> list[StateRevision]:
        target = group_id or (row.mutation_group_id if row is not None else None)
        if target is None and row is not None:
            return [row]
        return list(
            session.scalars(
                select(StateRevision)
                .where(StateRevision.mutation_group_id == target)
                .order_by(StateRevision.sequence)
            )
        )

    @staticmethod
    def _apply(session, revision: StateRevision, value: dict[str, Any] | None) -> None:
        if revision.entity_type == "campaign":
            row = session.get(Campaign, revision.entity_id)
        elif revision.entity_type == "character":
            row = session.get(Character, revision.entity_id)
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
            redoable=row.redoable,
            mutation_group_id=row.mutation_group_id,
        )
