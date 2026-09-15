"""Require the current self-contained snapshot record schema.

Revision ID: 20260814_29
Revises: 20260813_28
"""

from __future__ import annotations

import hashlib
import json
import zlib

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
    columns = {str(column["name"]) for column in inspector.get_columns("campaign_snapshots")}
    if columns == _CURRENT_COLUMNS:
        return
    if "payload" not in columns:
        raise RuntimeError(
            "unrecognized snapshot schema; preserve the database for explicit repair"
        )
    additions = [
        sa.Column("compressed_payload", sa.LargeBinary(), nullable=True),
        sa.Column("payload_codec", sa.String(32), nullable=True),
        sa.Column("uncompressed_size", sa.Integer(), nullable=True),
        sa.Column("record_checksum", sa.String(64), nullable=True),
    ]
    for column in additions:
        if column.name not in columns:
            op.add_column("campaign_snapshots", column)
    table = sa.Table("campaign_snapshots", sa.MetaData(), autoload_with=bind)
    for row in bind.execute(sa.select(table)).mappings():
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        checksum = hashlib.sha256(raw).hexdigest()
        if checksum != row["checksum"]:
            raise RuntimeError("historical snapshot checksum mismatch; migration rolled back")
        compressed = zlib.compress(raw)
        metadata = {
            "schema_version": row["schema_version"],
            "snapshot_id": row["id"],
            "campaign_id": row["campaign_id"],
            "branch_id": row["branch_id"],
            "parent_id": row["parent_id"],
            "slot": row["slot"],
            "payload_codec": "zlib-1",
            "uncompressed_size": len(raw),
            "payload_checksum": checksum,
        }
        encoded = json.dumps(
            metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        bind.execute(
            table.update()
            .where(table.c.id == row["id"])
            .values(
                compressed_payload=compressed,
                payload_codec="zlib-1",
                uncompressed_size=len(raw),
                record_checksum=hashlib.sha256(encoded + b"\0" + compressed).hexdigest(),
            )
        )
    # Native SQLite column removal preserves foreign keys and ledger references.
    for column in additions:
        if bind.dialect.name != "sqlite":
            op.alter_column("campaign_snapshots", column.name, nullable=False)
    op.drop_column("campaign_snapshots", "payload")


def downgrade() -> None:
    raise RuntimeError("the current snapshot record schema cannot be downgraded")
