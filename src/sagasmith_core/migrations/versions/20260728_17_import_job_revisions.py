"""Add optimistic revisions to import workflow state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_17"
down_revision = "20260725_16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("import_jobs"):
        return
    columns = {str(item["name"]) for item in inspector.get_columns("import_jobs")}
    if "revision" in columns:
        return
    with op.batch_alter_table("import_jobs") as batch:
        batch.add_column(
            sa.Column(
                "revision",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("import_jobs"):
        return
    columns = {str(item["name"]) for item in inspector.get_columns("import_jobs")}
    if "revision" not in columns:
        return
    with op.batch_alter_table("import_jobs") as batch:
        batch.drop_column("revision")
