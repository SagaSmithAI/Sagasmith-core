"""Add non-destructive branches and actor knowledge ledgers."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from sagasmith_core.models import Base

revision = "20260713_07"
down_revision = "20260712_06"
branch_labels = None
depends_on = None


def _add(table: str, column: sa.Column) -> None:
    inspector = sa.inspect(op.get_bind())
    if table in inspector.get_table_names():
        existing = {item["name"] for item in inspector.get_columns(table)}
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)
    _add("campaigns", sa.Column("active_branch_id", sa.String(36), nullable=True))
    _add("campaign_snapshots", sa.Column("branch_id", sa.String(36), nullable=True))
    _add("campaign_events", sa.Column("branch_id", sa.String(36), nullable=True))
    _add("campaign_events", sa.Column("committed_snapshot_id", sa.String(36), nullable=True))
    _add(
        "campaign_events",
        sa.Column("audience_scope", sa.String(200), nullable=False, server_default="dm"),
    )

def downgrade() -> None:
    # Branch and knowledge history is intentionally retained for user campaign safety.
    pass
