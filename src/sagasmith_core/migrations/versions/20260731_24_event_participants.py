"""Index actor participation in immutable campaign events."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260731_24"
down_revision = "20260728_23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("campaign_event_participants"):
        return
    op.create_table(
        "campaign_event_participants",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["campaign_events.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", "actor_id", "role"),
        sa.UniqueConstraint(
            "event_id",
            "actor_id",
            "role",
            name="uq_campaign_event_participant_role",
        ),
    )
    op.create_index(
        "ix_campaign_event_participant_actor",
        "campaign_event_participants",
        ["actor_id", "event_id"],
        unique=False,
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("campaign_event_participants"):
        return
    op.drop_index(
        "ix_campaign_event_participant_actor",
        table_name="campaign_event_participants",
    )
    op.drop_table("campaign_event_participants")
