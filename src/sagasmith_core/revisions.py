"""Audited state revisions with campaign-local undo and redo."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.idempotency import IdempotencyService, IdempotencyWrite
from sagasmith_core.models import (
    ActorGrant,
    ActorKnowledge,
    AuditLog,
    Campaign,
    CampaignEventParticipant,
    Character,
    ModuleActorBinding,
    MutationGroup,
    StateRevision,
)
from sagasmith_core.state_documents import load_state_document, persist_state_documents

REVERSIBLE_ENTITY_TYPES = frozenset({"campaign", "character", "actor_lifecycle"})


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
    idempotency_key: str | None = None
    request_hash: str | None = None
    reversible: bool = True


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
        reversible: bool = True,
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
                reversible=reversible,
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
        reversible: bool = True,
    ) -> list[RevisionInfo]:
        """Record a group inside an existing state transaction."""
        if not changes:
            raise ValueError("mutation group must contain at least one change")
        unsupported = sorted(
            {
                str(change.get("entity_type") or "")
                for change in changes
                if str(change.get("entity_type") or "") not in REVERSIBLE_ENTITY_TYPES
            }
        )
        if unsupported:
            raise ValueError(
                "revision groups support only reversible campaign/character documents; "
                "unsupported entities: " + ", ".join(unsupported)
            )
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
            reversible=reversible,
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
        document_ids = persist_state_documents(
            session,
            [
                value
                for change in changes
                for value in (change.get("before"), change.get("after"))
            ],
        )
        document_pairs = list(zip(document_ids[::2], document_ids[1::2], strict=True))
        for offset, (change, (before_document_id, after_document_id)) in enumerate(
            zip(changes, document_pairs, strict=True)
        ):
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
                before_document_id=before_document_id,
                after_document_id=after_document_id,
            )
            session.add(row)
            session.flush()
            self._audit(session, row, actor=actor)
            rows.append(row)
            parent_id = row.id
        session.flush()
        return [self._info(row) for row in rows]

    def undo(
        self,
        campaign_id: str,
        *,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> RevisionInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(session, idempotency_key, idempotency_write)
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
            group = (
                session.get(MutationGroup, row.mutation_group_id)
                if row.mutation_group_id
                else None
            )
            if group is not None and not group.reversible:
                raise ValueError(
                    "latest mutation is not reversible; restore a snapshot or branch"
                )
            rows = self._group_rows(session, row)
            for member in sorted(rows, key=lambda item: item.sequence, reverse=True):
                self._apply(
                    session,
                    member,
                    self._payload_value(session, member, before=True),
                )
                member.applied = False
                self._audit(session, member, actor="undo", reverse=True)
            if row.mutation_group_id:
                group = session.get(MutationGroup, row.mutation_group_id)
                if group is not None:
                    group.applied = False
            session.flush()
            result = self._info(row)
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

    def redo(
        self,
        campaign_id: str,
        *,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> RevisionInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(session, idempotency_key, idempotency_write)
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
            group_revision_sequence = (
                select(func.min(StateRevision.sequence))
                .where(StateRevision.mutation_group_id == MutationGroup.id)
                .scalar_subquery()
            )
            statement = select(MutationGroup).where(
                MutationGroup.campaign_id == campaign_id,
                MutationGroup.branch_id == branch_id,
                MutationGroup.applied.is_(False),
                MutationGroup.redoable.is_(True),
            )
            if current_group_id:
                statement = statement.where(group_revision_sequence > current.sequence)
            group = session.scalar(statement.order_by(group_revision_sequence).limit(1))
            if group is None:
                raise LookupError("nothing to redo")
            if not group.reversible:
                raise ValueError(
                    "next mutation is not reversible; restore a snapshot or branch"
                )
            rows = self._group_rows(session, group_id=group.id)
            for member in sorted(rows, key=lambda item: item.sequence):
                self._apply(
                    session,
                    member,
                    self._payload_value(session, member, before=False),
                )
                member.applied = True
                self._audit(session, member, actor="redo")
            group.applied = True
            row = rows[-1]
            session.flush()
            result = self._info(row)
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

    def history(
        self, campaign_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[RevisionInfo]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer between 1 and 500")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 100_000
        ):
            raise ValueError("offset must be an integer between 0 and 100000")
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            rows = session.execute(
                select(StateRevision)
                .add_columns(MutationGroup)
                .join(MutationGroup, MutationGroup.id == StateRevision.mutation_group_id)
                .where(
                    StateRevision.campaign_id == campaign_id,
                    self._visible_branch_revision_clause(session, campaign),
                )
                .order_by(StateRevision.sequence.desc())
                .offset(offset)
                .limit(limit)
            )
            return [
                self._info(revision, mutation_group=mutation_group)
                for revision, mutation_group in rows
            ]

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
        if revision.entity_type == "actor_lifecycle":
            RevisionService._apply_actor_lifecycle(session, revision, value)
            return
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
    def _apply_actor_lifecycle(
        session, revision: StateRevision, value: dict[str, Any] | None
    ) -> None:
        row = session.get(Character, revision.entity_id)
        if value is None:
            if row is None:
                raise LookupError(revision.entity_id)
            external_reference = any(
                session.scalar(statement) is not None
                for statement in (
                    select(ActorKnowledge.id).where(
                        ActorKnowledge.campaign_id == revision.campaign_id,
                        ActorKnowledge.actor_id == revision.entity_id,
                    ).limit(1),
                    select(CampaignEventParticipant.event_id).where(
                        CampaignEventParticipant.actor_id == revision.entity_id
                    ).limit(1),
                    select(ModuleActorBinding.id).where(
                        ModuleActorBinding.character_id == revision.entity_id
                    ).limit(1),
                    select(Character.id).where(
                        Character.template_id == revision.entity_id
                    ).limit(1),
                )
            )
            if external_reference:
                raise ValueError(
                    "actor lifecycle undo is blocked by an external actor reference"
                )
            session.execute(
                delete(ActorGrant).where(
                    ActorGrant.campaign_id == revision.campaign_id,
                    ActorGrant.actor_id == revision.entity_id,
                )
            )
            session.delete(row)
            session.flush()
            return
        if row is not None:
            raise ValueError("actor lifecycle redo requires the actor to be absent")
        character = dict(value.get("character") or {})
        expected_fields = {
            "id",
            "system_id",
            "campaign_id",
            "template_id",
            "character_type",
            "name",
            "player_name",
            "summary",
            "sheet",
            "notes",
            "revision",
        }
        if set(character) != expected_fields or character.get("id") != revision.entity_id:
            raise ValueError("actor lifecycle revision contains an invalid character document")
        row = Character(**character)
        session.add(row)
        session.flush()
        for grant in list(value.get("grants") or []):
            session.add(
                ActorGrant(
                    campaign_id=revision.campaign_id,
                    principal_id=str(grant["principal_id"]),
                    actor_id=revision.entity_id,
                    can_control=bool(grant.get("can_control")),
                    can_view_private=bool(grant.get("can_view_private")),
                )
            )
        session.flush()

    @staticmethod
    def _payload_value(
        session,
        revision: StateRevision,
        *,
        before: bool,
    ) -> dict[str, Any] | None:
        document_id = revision.before_document_id if before else revision.after_document_id
        return load_state_document(session, document_id)

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
                # The immutable revision row owns the reversible payload. Audit
                # entries retain its id and operation metadata without storing a
                # second copy of the same potentially large JSON documents.
                before=None,
                after=None,
            )
        )

    @staticmethod
    def _info(
        row: StateRevision,
        *,
        mutation_group: MutationGroup | None = None,
    ) -> RevisionInfo:
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
            idempotency_key=(
                mutation_group.idempotency_key if mutation_group is not None else None
            ),
            request_hash=(mutation_group.request_hash if mutation_group is not None else None),
            reversible=(mutation_group.reversible if mutation_group is not None else True),
        )
