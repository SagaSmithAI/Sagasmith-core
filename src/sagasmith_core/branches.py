"""Non-destructive campaign timeline branches."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import (
    BranchActorKnowledgeHead,
    BranchFactHead,
    Campaign,
    CampaignBranch,
    CampaignRuleActivation,
    CampaignSnapshot,
    MutationGroup,
    RuleResolutionReceipt,
    SnapshotActorKnowledgeBinding,
    SnapshotFactBinding,
    StateRevision,
)


@dataclass(frozen=True)
class BranchInfo:
    id: str
    campaign_id: str
    name: str
    base_snapshot_id: str | None
    head_snapshot_id: str | None
    is_current: bool


def resolve_branch(
    session: Session, campaign: Campaign, branch_id: str | None = None
) -> CampaignBranch:
    """Return an initialized branch for this campaign."""

    target_id = branch_id or campaign.active_branch_id
    row = session.get(CampaignBranch, target_id) if target_id else None
    if row is not None and row.campaign_id == campaign.id:
        return row

    row = session.scalar(
        select(CampaignBranch)
        .where(CampaignBranch.campaign_id == campaign.id, CampaignBranch.is_current.is_(True))
        .order_by(CampaignBranch.created_at, CampaignBranch.id)
    )
    if row is not None:
        campaign.active_branch_id = row.id
        return row

    raise LookupError(
        f"Campaign {campaign.id} has no branch. "
        "Create a new campaign or initialize a branch explicitly."
    )


class BranchService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def current(self, campaign_id: str) -> BranchInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            return self._info(resolve_branch(session, campaign))

    def list(self, campaign_id: str) -> list[BranchInfo]:
        with self.database.transaction() as session:
            if session.get(Campaign, campaign_id) is None:
                raise CampaignNotFoundError(campaign_id)
            return [
                self._info(row)
                for row in session.scalars(
                    select(CampaignBranch)
                    .where(CampaignBranch.campaign_id == campaign_id)
                    .order_by(CampaignBranch.created_at, CampaignBranch.id)
                )
            ]

    def get(self, campaign_id: str, branch_id: str) -> BranchInfo:
        with self.database.transaction() as session:
            if session.get(Campaign, campaign_id) is None:
                raise CampaignNotFoundError(campaign_id)
            row = session.get(CampaignBranch, branch_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(branch_id)
            return self._info(row)

    def compare(
        self, campaign_id: str, left_branch_id: str, right_branch_id: str
    ) -> dict[str, object]:
        """Compare branch heads without silently merging subjective knowledge."""
        with self.database.transaction() as session:
            if session.get(Campaign, campaign_id) is None:
                raise CampaignNotFoundError(campaign_id)
            left = session.get(CampaignBranch, left_branch_id)
            right = session.get(CampaignBranch, right_branch_id)
            if (
                left is None
                or right is None
                or left.campaign_id != campaign_id
                or right.campaign_id != campaign_id
            ):
                raise LookupError("branch does not belong to campaign")
            left_facts = {
                row.memory_id: row.revision_id
                for row in session.scalars(
                    select(BranchFactHead).where(BranchFactHead.branch_id == left.id)
                )
            }
            right_facts = {
                row.memory_id: row.revision_id
                for row in session.scalars(
                    select(BranchFactHead).where(BranchFactHead.branch_id == right.id)
                )
            }
            left_knowledge = {
                row.knowledge_id: row.revision_id
                for row in session.scalars(
                    select(BranchActorKnowledgeHead).where(
                        BranchActorKnowledgeHead.branch_id == left.id
                    )
                )
            }
            right_knowledge = {
                row.knowledge_id: row.revision_id
                for row in session.scalars(
                    select(BranchActorKnowledgeHead).where(
                        BranchActorKnowledgeHead.branch_id == right.id
                    )
                )
            }
            left_rules = {
                row.pack_id: f"{row.version}:{row.checksum}:{int(row.enabled)}"
                for row in session.scalars(
                    select(CampaignRuleActivation).where(
                        CampaignRuleActivation.branch_id == left.id
                    )
                )
            }
            right_rules = {
                row.pack_id: f"{row.version}:{row.checksum}:{int(row.enabled)}"
                for row in session.scalars(
                    select(CampaignRuleActivation).where(
                        CampaignRuleActivation.branch_id == right.id
                    )
                )
            }
            return {
                "campaign_id": campaign_id,
                "left_branch_id": left.id,
                "right_branch_id": right.id,
                "facts": self._diff_ids(left_facts, right_facts),
                "actor_knowledge": self._diff_ids(left_knowledge, right_knowledge),
                "rule_lock": self._diff_ids(left_rules, right_rules),
                "merge_policy": "explicit-per-fact-actor-knowledge-and-rule-lock",
            }

    @staticmethod
    def _diff_ids(left: dict[str, str], right: dict[str, str]) -> dict[str, list[str]]:
        return {
            "left_only": sorted(set(left) - set(right)),
            "right_only": sorted(set(right) - set(left)),
            "changed": sorted(key for key in set(left) & set(right) if left[key] != right[key]),
        }

    def create(
        self,
        campaign_id: str,
        *,
        name: str,
        from_snapshot_id: str | None = None,
        checkout: bool = False,
    ) -> BranchInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            current = resolve_branch(session, campaign) if campaign.active_branch_id else None
            source_id = from_snapshot_id or (current.head_snapshot_id if current else None)
            if current is not None and source_id is None:
                raise ValueError("create a snapshot before branching")
            if source_id:
                source = session.get(CampaignSnapshot, source_id)
                if source is None or source.campaign_id != campaign_id:
                    raise LookupError(source_id)
                from sagasmith_core.snapshots import SnapshotService

                SnapshotService._assert_integrity(session, source)
            if checkout and current is not None:
                SnapshotService._assert_clean_branch(session, campaign, current)
            row = CampaignBranch(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                name=name,
                base_snapshot_id=source_id,
                head_snapshot_id=source_id,
                is_current=current is None,
            )
            session.add(row)
            session.flush()
            if source_id:
                self._copy_snapshot_heads(session, source_id, row.id)
                self._copy_snapshot_revisions(session, source_id, row.id)
                if not checkout:
                    self._copy_snapshot_rule_lock(session, source_id, row.id)
            elif current is not None:
                self._copy_branch_heads(session, current.id, row.id)
                self._copy_branch_rule_lock(session, current.id, row.id)
            if current is None:
                campaign.active_branch_id = row.id
            elif checkout:
                self._checkout(session, campaign, row)
                if row.head_snapshot_id:
                    # Keep direct BranchService callers safe too: pointer
                    # switching and snapshot materialization share this
                    # transaction.  The local import avoids the module cycle
                    # with SnapshotService's branch helpers.
                    from sagasmith_core.snapshots import (
                        SnapshotService,
                    )

                    snapshot = session.get(CampaignSnapshot, row.head_snapshot_id)
                    if snapshot is None or snapshot.campaign_id != campaign_id:
                        raise LookupError(row.head_snapshot_id)
                    SnapshotService._assert_integrity(session, snapshot)
                    SnapshotService(self.database)._apply(session, campaign, dict(snapshot.payload))
            return self._info(row)

    def checkout(self, campaign_id: str, branch_id: str) -> BranchInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            row = session.get(CampaignBranch, branch_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(branch_id)
            current = resolve_branch(session, campaign)
            if current.id == row.id:
                return self._info(row)
            from sagasmith_core.snapshots import SnapshotIntegrityError, SnapshotService

            SnapshotService._assert_clean_branch(session, campaign, current)
            if row.head_snapshot_id is None:
                raise SnapshotIntegrityError("cannot checkout a branch without a snapshot head")
            self._checkout(session, campaign, row)
            if row.head_snapshot_id:
                # Direct core callers must receive the same atomic pointer plus
                # materialized state contract as SnapshotService.checkout_branch.
                snapshot = session.get(CampaignSnapshot, row.head_snapshot_id)
                if snapshot is None or snapshot.campaign_id != campaign_id:
                    raise LookupError(row.head_snapshot_id)
                SnapshotService._assert_integrity(session, snapshot)
                SnapshotService(self.database)._apply(session, campaign, dict(snapshot.payload))
            return self._info(row)

    @staticmethod
    def _checkout(session: Session, campaign: Campaign, branch: CampaignBranch) -> None:
        session.execute(
            update(CampaignBranch)
            .where(CampaignBranch.campaign_id == campaign.id)
            .values(is_current=False)
        )
        branch.is_current = True
        campaign.active_branch_id = branch.id

    @staticmethod
    def _copy_snapshot_heads(session: Session, snapshot_id: str, branch_id: str) -> None:
        facts = list(
            session.scalars(
                select(SnapshotFactBinding).where(SnapshotFactBinding.snapshot_id == snapshot_id)
            )
        )
        for item in facts:
            session.add(
                BranchFactHead(
                    branch_id=branch_id, memory_id=item.memory_id, revision_id=item.revision_id
                )
            )
        for item in session.scalars(
            select(SnapshotActorKnowledgeBinding).where(
                SnapshotActorKnowledgeBinding.snapshot_id == snapshot_id
            )
        ):
            session.add(
                BranchActorKnowledgeHead(
                    branch_id=branch_id,
                    knowledge_id=item.knowledge_id,
                    revision_id=item.revision_id,
                )
            )

    @staticmethod
    def _copy_snapshot_revisions(session: Session, snapshot_id: str, branch_id: str) -> None:
        """Fork the snapshot's reversible cursor so undo never mutates its source branch."""
        snapshot = session.get(CampaignSnapshot, snapshot_id)
        if snapshot is None:
            return
        cursor = {
            str(item["id"]): item
            for item in dict(snapshot.payload).get("revision_cursor", [])
            if item.get("id")
        }
        if not cursor:
            return
        rows = list(
            session.scalars(
                select(StateRevision)
                .where(StateRevision.id.in_(cursor))
                .order_by(StateRevision.sequence)
            )
        )
        if not rows:
            return
        max_sequence = (
            session.scalar(
                select(func.max(StateRevision.sequence)).where(
                    StateRevision.campaign_id == snapshot.campaign_id
                )
            )
            or 0
        )
        group_ids = {row.mutation_group_id for row in rows if row.mutation_group_id}
        group_map: dict[str, str] = {}
        for group_offset, old_group in enumerate(
            session.scalars(select(MutationGroup).where(MutationGroup.id.in_(group_ids))), start=1
        ):
            group_rows = [row for row in rows if row.mutation_group_id == old_group.id]
            new_id = str(uuid.uuid4())
            group_map[old_group.id] = new_id
            session.add(
                MutationGroup(
                    id=new_id,
                    campaign_id=old_group.campaign_id,
                    branch_id=branch_id,
                    sequence=max_sequence + group_offset,
                    operation=old_group.operation,
                    actor=old_group.actor,
                    idempotency_key=None,
                    request_hash=None,
                    applied=all(bool(cursor[row.id].get("applied", True)) for row in group_rows),
                    redoable=all(bool(cursor[row.id].get("redoable", True)) for row in group_rows),
                )
            )
        for old_receipt in session.scalars(
            select(RuleResolutionReceipt).where(
                RuleResolutionReceipt.mutation_group_id.in_(group_ids)
            )
        ):
            new_group_id = group_map.get(old_receipt.mutation_group_id)
            if new_group_id is None:
                continue
            session.add(
                RuleResolutionReceipt(
                    id=str(uuid.uuid4()),
                    campaign_id=old_receipt.campaign_id,
                    branch_id=branch_id,
                    mutation_group_id=new_group_id,
                    ruleset_fingerprint=old_receipt.ruleset_fingerprint,
                    mechanic_id=old_receipt.mechanic_id,
                    event=old_receipt.event,
                    receipt=dict(old_receipt.receipt),
                    created_at=old_receipt.created_at,
                )
            )
        revision_map = {row.id: str(uuid.uuid4()) for row in rows}
        for offset, old in enumerate(rows, start=1):
            cursor_item = cursor[old.id]
            session.add(
                StateRevision(
                    id=revision_map[old.id],
                    mutation_group_id=group_map.get(old.mutation_group_id),
                    campaign_id=old.campaign_id,
                    parent_id=revision_map.get(old.parent_id),
                    sequence=max_sequence + offset,
                    # Preserve the source revision id as a snapshot-cursor alias.
                    branch_key=old.id,
                    operation=old.operation,
                    entity_type=old.entity_type,
                    entity_id=old.entity_id,
                    before=dict(old.before) if old.before else None,
                    after=dict(old.after) if old.after else None,
                    applied=bool(cursor_item.get("applied", True)),
                    redoable=bool(cursor_item.get("redoable", True)),
                )
            )

    @staticmethod
    def _copy_snapshot_rule_lock(session: Session, snapshot_id: str, branch_id: str) -> None:
        snapshot = session.get(CampaignSnapshot, snapshot_id)
        if snapshot is None:
            return
        for item in dict(snapshot.payload).get("rule_lock", []):
            session.add(
                CampaignRuleActivation(
                    campaign_id=snapshot.campaign_id,
                    branch_id=branch_id,
                    pack_id=item["pack_id"],
                    version=item["version"],
                    checksum=item["checksum"],
                    enabled=bool(item.get("enabled", True)),
                    options=dict(item.get("options") or {}),
                )
            )

    @staticmethod
    def _copy_branch_heads(session: Session, source_id: str, branch_id: str) -> None:
        for item in session.scalars(
            select(BranchFactHead).where(BranchFactHead.branch_id == source_id)
        ):
            session.add(
                BranchFactHead(
                    branch_id=branch_id, memory_id=item.memory_id, revision_id=item.revision_id
                )
            )
        for item in session.scalars(
            select(BranchActorKnowledgeHead).where(BranchActorKnowledgeHead.branch_id == source_id)
        ):
            session.add(
                BranchActorKnowledgeHead(
                    branch_id=branch_id,
                    knowledge_id=item.knowledge_id,
                    revision_id=item.revision_id,
                )
            )

    @staticmethod
    def _copy_branch_rule_lock(session: Session, source_id: str, branch_id: str) -> None:
        for item in session.scalars(
            select(CampaignRuleActivation).where(CampaignRuleActivation.branch_id == source_id)
        ):
            session.add(
                CampaignRuleActivation(
                    campaign_id=item.campaign_id,
                    branch_id=branch_id,
                    pack_id=item.pack_id,
                    version=item.version,
                    checksum=item.checksum,
                    enabled=item.enabled,
                    options=dict(item.options),
                )
            )

    @staticmethod
    def _info(row: CampaignBranch) -> BranchInfo:
        return BranchInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            name=row.name,
            base_snapshot_id=row.base_snapshot_id,
            head_snapshot_id=row.head_snapshot_id,
            is_current=row.is_current,
        )
