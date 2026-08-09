"""Remove stored snapshot heads and retire chapter play-state labels."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_21"
down_revision = "20260728_20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "campaign_snapshots" in tables:
        columns = {str(item["name"]) for item in inspector.get_columns("campaign_snapshots")}
        if "is_head" in columns:
            indexes = {
                str(item["name"])
                for item in inspector.get_indexes("campaign_snapshots")
                if item.get("name")
            }
            with op.batch_alter_table("campaign_snapshots") as batch:
                if "ix_campaign_snapshot_head" in indexes:
                    batch.drop_index("ix_campaign_snapshot_head")
                batch.drop_column("is_head")
    if "module_chapters" in tables:
        op.execute(
            "UPDATE module_chapters SET status = 'indexed' WHERE status IN ('current', 'locked')"
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("campaign_snapshots"):
        return
    columns = {str(item["name"]) for item in inspector.get_columns("campaign_snapshots")}
    if "is_head" not in columns:
        with op.batch_alter_table("campaign_snapshots") as batch:
            batch.add_column(
                sa.Column(
                    "is_head",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
            batch.create_index(
                "ix_campaign_snapshot_head",
                ["campaign_id", "is_head"],
            )
