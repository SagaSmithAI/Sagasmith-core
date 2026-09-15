"""Add grouped revisions, idempotency, and principal/actor grants."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from sagasmith_core.migrations.schema_20260714_08 import Base

revision = "20260714_08"
down_revision = "20260713_07"
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
    # New installations get the complete schema. Existing rows are intentionally
    # not backfilled; new writes use the new integrity surfaces only.
    Base.metadata.create_all(bind=bind, checkfirst=True)
    _add(
        "state_revisions",
        sa.Column("mutation_group_id", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    # Runtime history, grants, and idempotency records are retained intentionally.
    pass
