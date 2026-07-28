"""Make the campaign branch pointer the sole current-branch authority."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_22"
down_revision = "20260728_21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if not {"campaigns", "campaign_branches"}.issubset(tables):
        return
    columns = {
        str(item["name"])
        for item in inspector.get_columns("campaign_branches")
    }
    if "is_current" not in columns:
        return
    op.execute(
        """
        UPDATE campaigns
        SET active_branch_id = (
            SELECT branch.id
            FROM campaign_branches AS branch
            WHERE branch.campaign_id = campaigns.id
            ORDER BY
                CASE WHEN branch.is_current = 1 THEN 0 ELSE 1 END,
                branch.created_at,
                branch.id
            LIMIT 1
        )
        WHERE active_branch_id IS NULL
        """
    )
    indexes = {
        str(item["name"])
        for item in inspector.get_indexes("campaign_branches")
        if item.get("name")
    }
    with op.batch_alter_table("campaign_branches") as batch:
        if "ix_campaign_branch_current" in indexes:
            batch.drop_index("ix_campaign_branch_current")
        batch.drop_column("is_current")


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("campaign_branches"):
        return
    columns = {
        str(item["name"])
        for item in inspector.get_columns("campaign_branches")
    }
    if "is_current" in columns:
        return
    with op.batch_alter_table("campaign_branches") as batch:
        batch.add_column(
            sa.Column(
                "is_current",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.create_index(
            "ix_campaign_branch_current",
            ["campaign_id", "is_current"],
        )
    op.execute(
        """
        UPDATE campaign_branches
        SET is_current = 1
        WHERE id IN (
            SELECT active_branch_id
            FROM campaigns
            WHERE active_branch_id IS NOT NULL
        )
        """
    )
