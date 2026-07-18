"""Campaign-scoped event log."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from sagasmith_core.branches import resolve_branch
from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import Campaign, CampaignEvent, SnapshotEventBinding

_AUDIENCE_SCOPES = {"dm", "public", "party", "player", "actor"}


@dataclass(frozen=True)
class CampaignEventInfo:
    id: str
    campaign_id: str
    sequence: int
    event_type: str
    summary: str
    payload: dict[str, Any]
    audience_scope: str
    created_at: str


class EventService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self,
        campaign_id: str,
        *,
        event_type: str = "narrative",
        summary: str,
        payload: dict[str, Any] | None = None,
        audience_scope: str = "dm",
        branch_id: str | None = None,
    ) -> CampaignEventInfo:
        if audience_scope not in _AUDIENCE_SCOPES:
            raise ValueError(f"invalid event audience scope: {audience_scope}")
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            sequence = (
                session.scalar(
                    select(func.max(CampaignEvent.sequence)).where(
                        CampaignEvent.campaign_id == campaign_id
                    )
                )
                or 0
            ) + 1
            row = CampaignEvent(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                sequence=sequence,
                event_type=event_type,
                summary=summary,
                payload=payload or {},
                audience_scope=audience_scope,
                branch_id=branch.id,
            )
            session.add(row)
            session.flush()
            return self._info(row)

    def list(
        self, campaign_id: str, *, limit: int = 50, branch_id: str | None = None
    ) -> list[CampaignEventInfo]:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            bound_ids: set[str] = set()
            if branch.head_snapshot_id:
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
                    session.scalars(select(CampaignEvent).where(CampaignEvent.id.in_(bound_ids)))
                )
            rows.extend(
                session.scalars(
                    select(CampaignEvent).where(
                        CampaignEvent.campaign_id == campaign_id,
                        CampaignEvent.branch_id == branch.id,
                        CampaignEvent.committed_snapshot_id.is_(None),
                    )
                )
            )
            rows = sorted(rows, key=lambda row: (row.sequence, row.id))[-max(1, min(limit, 500)) :]
            return [self._info(row) for row in rows]

    @staticmethod
    def _info(row: CampaignEvent) -> CampaignEventInfo:
        return CampaignEventInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            sequence=row.sequence,
            event_type=row.event_type,
            summary=row.summary,
            payload=dict(row.payload),
            audience_scope=row.audience_scope,
            created_at=row.created_at.isoformat(),
        )
