"""Add map scene, token, and region documents."""

from alembic import op

from sagasmith_core.models import Base

revision = "20260707_05"
down_revision = "20260706_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for table in ("scene_regions", "scene_tokens", "map_scenes"):
        op.drop_table(table)
