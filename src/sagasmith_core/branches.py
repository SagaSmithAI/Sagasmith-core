"""Non-destructive campaign timeline branches."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import (
    BranchActorKnowledgeHead,
    BranchFactHead,
    Campaign,
    CampaignBranch,
    CampaignSnapshot,
    SnapshotActorKnowledgeBinding,
    SnapshotFactBinding,
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
            if source_id:
                source = session.get(CampaignSnapshot, source_id)
                if source is None or source.campaign_id != campaign_id:
                    raise LookupError(source_id)
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
            elif current is not None:
                self._copy_branch_heads(session, current.id, row.id)
            if current is None:
                campaign.active_branch_id = row.id
            elif checkout:
                self._checkout(session, campaign, row)
            return self._info(row)

    def checkout(self, campaign_id: str, branch_id: str) -> BranchInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            row = session.get(CampaignBranch, branch_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(branch_id)
            self._checkout(session, campaign, row)
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
    def _info(row: CampaignBranch) -> BranchInfo:
        return BranchInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            name=row.name,
            base_snapshot_id=row.base_snapshot_id,
            head_snapshot_id=row.head_snapshot_id,
            is_current=row.is_current,
        )
