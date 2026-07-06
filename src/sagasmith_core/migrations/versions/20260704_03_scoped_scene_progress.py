"""Allow parties, split groups, and individual players to track different scenes."""

import sqlalchemy as sa
from alembic import op

revision = "20260704_03"
down_revision = "20260701_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("scene_progress")}
    if "scope_id" in columns:
        # Fresh databases are created from current metadata by the initial migration.
        return

    with op.batch_alter_table("scene_progress") as batch:
        batch.add_column(
            sa.Column(
                "scope_id",
                sa.String(200),
                nullable=False,
                server_default="party",
            )
        )
        batch.drop_constraint("uq_scene_progress", type_="unique")
        batch.create_unique_constraint(
            "uq_scene_progress",
            ["campaign_id", "scope_id", "scene_id"],
        )
        batch.create_index("ix_scene_progress_scope_id", ["scope_id"])


def downgrade() -> None:
    # Multiple scopes cannot be collapsed without losing campaign state.
    pass
