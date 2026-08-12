"""Mark mutations that require snapshot or branch recovery.

Revision ID: 20260813_28
Revises: 20260802_27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260813_28"
down_revision = "20260802_27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("mutation_groups"):
        return
    columns = {column["name"] for column in inspector.get_columns("mutation_groups")}
    if "reversible" not in columns:
        with op.batch_alter_table("mutation_groups") as batch:
            batch.add_column(
                sa.Column(
                    "reversible",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                )
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("mutation_groups"):
        return
    columns = {column["name"] for column in inspector.get_columns("mutation_groups")}
    if "reversible" in columns:
        with op.batch_alter_table("mutation_groups") as batch:
            batch.drop_column("reversible")
