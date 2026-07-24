"""Scope mutation idempotency keys to one campaign branch."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260725_16"
down_revision = "20260723_15"
branch_labels = None
depends_on = None


def _unique_constraints(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_unique_constraints(table)
        if item.get("name")
    }


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("mutation_groups"):
        return
    constraints = _unique_constraints("mutation_groups")
    if "uq_mutation_group_branch_idempotency" in constraints:
        return
    with op.batch_alter_table("mutation_groups") as batch:
        if "uq_mutation_group_campaign_idempotency" in constraints:
            batch.drop_constraint(
                "uq_mutation_group_campaign_idempotency",
                type_="unique",
            )
        batch.create_unique_constraint(
            "uq_mutation_group_branch_idempotency",
            ["campaign_id", "branch_id", "idempotency_key"],
        )


def downgrade() -> None:
    constraints = _unique_constraints("mutation_groups")
    if "uq_mutation_group_branch_idempotency" not in constraints:
        return
    with op.batch_alter_table("mutation_groups") as batch:
        batch.drop_constraint(
            "uq_mutation_group_branch_idempotency",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_mutation_group_campaign_idempotency",
            ["campaign_id", "idempotency_key"],
        )
