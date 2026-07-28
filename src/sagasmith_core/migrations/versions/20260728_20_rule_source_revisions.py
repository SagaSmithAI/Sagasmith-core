"""Preserve immutable rule-source revisions across reimports."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_20"
down_revision = "20260728_19"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("rule_sources"):
        return set()
    return {str(item["name"]) for item in inspector.get_columns("rule_sources")}


def upgrade() -> None:
    columns = _columns()
    if not columns or "active" in columns:
        return
    with op.batch_alter_table("rule_sources") as batch:
        batch.add_column(
            sa.Column(
                "active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )


def downgrade() -> None:
    columns = _columns()
    if not columns or "active" not in columns:
        return
    with op.batch_alter_table("rule_sources") as batch:
        batch.drop_column("active")
