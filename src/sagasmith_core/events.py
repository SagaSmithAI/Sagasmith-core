"""Campaign-scoped event log."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import Campaign, CampaignEvent


@dataclass(frozen=True)
class CampaignEventInfo:
    id: str
    campaign_id: str
    sequence: int
    event_type: str
    summary: str
    payload: dict[str, Any]
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
    ) -> CampaignEventInfo:
        with self.database.transaction() as session:
            if session.get(Campaign, campaign_id) is None:
                raise CampaignNotFoundError(campaign_id)
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
            )
            session.add(row)
            session.flush()
            return self._info(row)

    def list(self, campaign_id: str, *, limit: int = 50) -> list[CampaignEventInfo]:
        with self.database.transaction() as session:
            if session.get(Campaign, campaign_id) is None:
                raise CampaignNotFoundError(campaign_id)
            statement = (
                select(CampaignEvent)
                .where(CampaignEvent.campaign_id == campaign_id)
                .order_by(CampaignEvent.sequence.desc())
                .limit(max(1, min(limit, 500)))
            )
            rows = list(session.scalars(statement))
            return [self._info(row) for row in reversed(rows)]

    @staticmethod
    def _info(row: CampaignEvent) -> CampaignEventInfo:
        return CampaignEventInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            sequence=row.sequence,
            event_type=row.event_type,
            summary=row.summary,
            payload=dict(row.payload),
            created_at=row.created_at.isoformat(),
        )
