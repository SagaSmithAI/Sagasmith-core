"""Add restore-branch visibility for state revisions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260712_05"
down_revision = "20260706_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "state_revisions" not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_columns("state_revisions")}
    if "redoable" not in existing:
        op.add_column(
            "state_revisions",
            sa.Column("redoable", sa.Boolean(), nullable=False, server_default=sa.true()),
        )


def downgrade() -> None:
    # Retain history when downgrading a user campaign database.
    pass
