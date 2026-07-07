"""Add Foundry-style runtime document tables."""

from alembic import op

from sagasmith_core.models import Base

revision = "20260707_06"
down_revision = "20260707_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for table in (
        "game_messages",
        "active_effects",
        "game_activities",
        "game_items",
        "game_actors",
    ):
        op.drop_table(table)
