"""Require the current self-contained snapshot record schema.

Revision ID: 20260814_29
Revises: 20260813_28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260814_29"
down_revision = "20260813_28"
branch_labels = None
depends_on = None

_CURRENT_COLUMNS = {
    "id",
    "campaign_id",
    "branch_id",
    "parent_id",
    "slot",
    "label",
    "schema_version",
    "compressed_payload",
    "payload_codec",
    "uncompressed_size",
    "checksum",
    "record_checksum",
    "recap",
    "created_at",
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("campaign_snapshots"):
        return
    columns = {
        str(column["name"])
        for column in inspector.get_columns("campaign_snapshots")
    }
    if columns != _CURRENT_COLUMNS:
        raise RuntimeError("campaign_snapshots must use the current snapshot record schema")


def downgrade() -> None:
    raise RuntimeError("the current snapshot record schema cannot be downgraded")
