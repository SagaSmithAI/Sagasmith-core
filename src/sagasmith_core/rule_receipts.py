"""Read-only access to persisted rule-resolution evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from sagasmith_core.database import Database
from sagasmith_core.models import MutationGroup, RuleResolutionReceipt


@dataclass(frozen=True)
class RuleReceiptInfo:
    id: str
    campaign_id: str
    branch_id: str | None
    mutation_group_id: str
    ruleset_fingerprint: str
    mechanic_id: str
    event: str
    receipt: dict[str, Any]
    operation: str
    sequence: int
    applied: bool
    redoable: bool
    created_at: datetime


class RuleReceiptService:
    """Query receipts without requiring the original pack to remain installed."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def list(
        self,
        campaign_id: str,
        *,
        branch_id: str | None = None,
        mechanic_id: str | None = None,
        limit: int = 100,
    ) -> list[RuleReceiptInfo]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.database.session_factory() as session:
            statement = select(RuleResolutionReceipt).where(
                RuleResolutionReceipt.campaign_id == campaign_id
            )
            if branch_id is not None:
                statement = statement.where(RuleResolutionReceipt.branch_id == branch_id)
            if mechanic_id is not None:
                statement = statement.where(RuleResolutionReceipt.mechanic_id == mechanic_id)
            rows = session.execute(
                statement.join(
                    MutationGroup,
                    MutationGroup.id == RuleResolutionReceipt.mutation_group_id,
                )
                .add_columns(MutationGroup)
                .order_by(RuleResolutionReceipt.created_at.desc())
                .limit(limit)
            )
            return [self._info(receipt, group) for receipt, group in rows]

    @staticmethod
    def _info(row: RuleResolutionReceipt, group: MutationGroup) -> RuleReceiptInfo:
        return RuleReceiptInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            branch_id=row.branch_id,
            mutation_group_id=row.mutation_group_id,
            ruleset_fingerprint=row.ruleset_fingerprint,
            mechanic_id=row.mechanic_id,
            event=row.event,
            receipt=dict(row.receipt),
            operation=group.operation,
            sequence=group.sequence,
            applied=group.applied,
            redoable=group.redoable,
            created_at=row.created_at,
        )
