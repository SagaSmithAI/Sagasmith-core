"""Add first-class bounded event retrieval text.

Revision ID: 20260815_33
Revises: 20260815_32
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260815_33"
down_revision = "20260815_32"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("campaign_events"):
        return
    columns = {str(column["name"]) for column in inspector.get_columns("campaign_events")}
    if "retrieval_text" in columns:
        return
    op.add_column(
        "campaign_events",
        sa.Column("retrieval_text", sa.Text(), nullable=False, server_default=""),
    )
    bind.execute(
        sa.text(
            "UPDATE campaign_events SET retrieval_text = summary "
            "WHERE retrieval_text = ''"
        )
    )


def downgrade() -> None:
    raise RuntimeError("event retrieval_text is part of the only supported protocol")
