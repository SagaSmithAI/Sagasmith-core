"""Make memory revision status the sole lifecycle authority."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_18"
down_revision = "20260728_17"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("memory_revisions"):
        return set()
    return {str(item["name"]) for item in inspector.get_columns("memory_revisions")}


def upgrade() -> None:
    columns = _columns()
    if "active" not in columns:
        return
    if "status" in columns:
        # The legacy Boolean could express only visible or inactive. Preserve
        # inactive rows conservatively as retracted when v15 supplied its
        # default active status during migration.
        op.execute(
            "UPDATE memory_revisions SET status = 'retracted' "
            "WHERE active = 0 AND status = 'active'"
        )
    with op.batch_alter_table("memory_revisions") as batch:
        batch.drop_column("active")


def downgrade() -> None:
    columns = _columns()
    if "active" in columns:
        return
    with op.batch_alter_table("memory_revisions") as batch:
        batch.add_column(
            sa.Column(
                "active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
    if "status" in columns:
        op.execute(
            "UPDATE memory_revisions SET active = "
            "CASE WHEN status = 'active' THEN 1 ELSE 0 END"
        )
