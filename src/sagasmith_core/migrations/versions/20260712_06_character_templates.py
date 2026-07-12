"""Track the library template used by a campaign character instance."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260712_06"
down_revision = "20260712_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "characters" not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_columns("characters")}
    if "template_id" not in existing:
        op.add_column(
            "characters",
            sa.Column("template_id", sa.String(length=36), nullable=True),
        )
        op.create_index("ix_characters_template_id", "characters", ["template_id"])


def downgrade() -> None:
    # Retain template provenance when downgrading a user campaign database.
    pass
