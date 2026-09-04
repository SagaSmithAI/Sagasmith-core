"""Read-only access to persisted rule-resolution evidence."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
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
    reversible: bool
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

    def has_applied_receipt(
        self,
        campaign_id: str,
        *,
        event: str,
        receipt_fields: Mapping[str, Any],
        branch_id: str | None = None,
    ) -> bool:
        """Return whether an applied receipt contains the requested top-level fields.

        The database narrows the authoritative candidates before receipt JSON is
        inspected.  Field matching is deliberately exact at the top level: every
        requested key must be present and its value must compare equal.
        """
        if not isinstance(receipt_fields, Mapping) or not receipt_fields:
            raise ValueError("receipt_fields must be a non-empty mapping")
        expected_fields = deepcopy(dict(receipt_fields))
        # Join an ambient mutation transaction when one exists. Callers that
        # authorize a dependent write must observe receipt state under the
        # same transaction/locks as that write, not through a second session.
        with self.database.transaction() as session:
            statement = (
                select(RuleResolutionReceipt.receipt)
                .join(
                    MutationGroup,
                    MutationGroup.id == RuleResolutionReceipt.mutation_group_id,
                )
                .where(
                    RuleResolutionReceipt.campaign_id == campaign_id,
                    MutationGroup.campaign_id == campaign_id,
                    RuleResolutionReceipt.event == event,
                    MutationGroup.applied.is_(True),
                )
            )
            if branch_id is not None:
                statement = statement.where(
                    RuleResolutionReceipt.branch_id == branch_id,
                    MutationGroup.branch_id == branch_id,
                )
            for (receipt,) in session.execute(statement):
                if isinstance(receipt, Mapping) and all(
                    key in receipt and receipt[key] == value
                    for key, value in expected_fields.items()
                ):
                    return True
        return False

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
            reversible=group.reversible,
            created_at=row.created_at,
        )
