"""Non-restorable decision identities for commands and durable continuations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sagasmith_core.database import Database
    from sagasmith_core.work import UnitOfWork


@dataclass(frozen=True)
class DecisionBase:
    campaign_id: str
    branch_id: str
    timeline_epoch: int
    campaign_revision: int

    @classmethod
    def capture(cls, database: Database, work: UnitOfWork, campaign_id: str):
        from sagasmith_core.models import Campaign

        with database.operation(work) as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None or campaign.active_branch_id is None:
                raise LookupError(campaign_id)
            return cls(
                campaign.id, campaign.active_branch_id, campaign.timeline_epoch, campaign.revision
            )

    def require_current(self, database: Database, work: UnitOfWork) -> None:
        from sagasmith_core.models import Campaign

        with database.operation(work) as session:
            campaign = session.get(Campaign, self.campaign_id, populate_existing=True)
            if campaign is None or (
                campaign.active_branch_id,
                campaign.timeline_epoch,
                campaign.revision,
            ) != (self.branch_id, self.timeline_epoch, self.campaign_revision):
                raise ValueError("stale command or continuation decision base")
