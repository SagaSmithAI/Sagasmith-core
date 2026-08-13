"""Replace materialized snapshot JSON with self-contained compressed records.

Revision ID: 20260814_29
Revises: 20260813_28
"""

from __future__ import annotations

import hashlib
import json
import zlib
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260814_29"
down_revision = "20260813_28"
branch_labels = None
depends_on = None

_CODEC = "zlib-1"
_TARGET_SCHEMA_VERSION = 8
_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
_REQUIRED_PAYLOAD_FIELDS = {
    "campaign",
    "rule_profile",
    "rule_lock",
    "addon_lock",
    "characters",
    "module_activations",
    "scene_progress",
    "events",
    "memories",
    "actor_knowledge",
    "revision_cursor",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _record_checksum(row: dict[str, Any], compressed: bytes, size: int) -> str:
    metadata = {
        "schema_version": _TARGET_SCHEMA_VERSION,
        "snapshot_id": row["id"],
        "campaign_id": row["campaign_id"],
        "branch_id": row["branch_id"],
        "parent_id": row["parent_id"],
        "slot": row["slot"],
        "payload_codec": _CODEC,
        "uncompressed_size": size,
        "payload_checksum": row["checksum"],
    }
    digest = hashlib.sha256()
    digest.update(_canonical_json(metadata).encode("utf-8"))
    digest.update(b"\0")
    digest.update(compressed)
    return digest.hexdigest()


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError("snapshot payload migration requires a JSON object")
    return value


def _validated_payload(row: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    if int(row["schema_version"]) != 7:
        raise RuntimeError(
            f"snapshot {row['id']} uses unsupported schema {row['schema_version']}; "
            "materialize it through a pinned historical runtime before migration"
        )
    payload = _payload(row["payload"])
    if not _REQUIRED_PAYLOAD_FIELDS.issubset(payload):
        raise RuntimeError(f"snapshot {row['id']} is not a complete schema-7 payload")
    if _json_checksum(payload) != row["checksum"]:
        raise RuntimeError(f"snapshot {row['id']} failed checksum preflight")
    raw = _canonical_json(payload).encode("utf-8")
    if len(raw) > _MAX_PAYLOAD_BYTES:
        raise RuntimeError(f"snapshot {row['id']} exceeds the migration size limit")
    return payload, raw


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("campaign_snapshots"):
        return
    columns = {str(item["name"]) for item in inspector.get_columns("campaign_snapshots")}
    new_columns = {
        "compressed_payload",
        "payload_codec",
        "uncompressed_size",
        "record_checksum",
    }
    if "payload" not in columns:
        missing = new_columns - columns
        if missing:
            raise RuntimeError(
                "snapshot compression migration found an incomplete current schema: "
                + ", ".join(sorted(missing))
            )
        return

    source_table = sa.table(
        "campaign_snapshots",
        sa.column("id", sa.String()),
        sa.column("campaign_id", sa.String()),
        sa.column("branch_id", sa.String()),
        sa.column("parent_id", sa.String()),
        sa.column("slot", sa.Integer()),
        sa.column("schema_version", sa.Integer()),
        sa.column("payload", sa.JSON()),
        sa.column("checksum", sa.String()),
    )
    # Reject unsupported, incomplete, oversized, or corrupt source rows before
    # the first schema mutation. This keeps the old protocol fully selectable
    # when preflight fails, especially on SQLite where DDL rollback is limited.
    for result in bind.execute(
        sa.select(source_table).order_by(source_table.c.slot)
    ).mappings():
        _validated_payload(dict(result))

    with op.batch_alter_table("campaign_snapshots") as batch:
        if "compressed_payload" not in columns:
            batch.add_column(sa.Column("compressed_payload", sa.LargeBinary(), nullable=True))
        if "payload_codec" not in columns:
            batch.add_column(sa.Column("payload_codec", sa.String(32), nullable=True))
        if "uncompressed_size" not in columns:
            batch.add_column(sa.Column("uncompressed_size", sa.Integer(), nullable=True))
        if "record_checksum" not in columns:
            batch.add_column(sa.Column("record_checksum", sa.String(64), nullable=True))

    snapshot_table = sa.table(
        "campaign_snapshots",
        sa.column("id", sa.String()),
        sa.column("campaign_id", sa.String()),
        sa.column("branch_id", sa.String()),
        sa.column("parent_id", sa.String()),
        sa.column("slot", sa.Integer()),
        sa.column("schema_version", sa.Integer()),
        sa.column("payload", sa.JSON()),
        sa.column("checksum", sa.String()),
        sa.column("compressed_payload", sa.LargeBinary()),
        sa.column("payload_codec", sa.String()),
        sa.column("uncompressed_size", sa.Integer()),
        sa.column("record_checksum", sa.String()),
    )
    rows = bind.execute(sa.select(snapshot_table).order_by(snapshot_table.c.slot)).mappings()
    for result in rows:
        row = dict(result)
        _, raw = _validated_payload(row)
        compressed = zlib.compress(raw)
        bind.execute(
            snapshot_table.update()
            .where(snapshot_table.c.id == row["id"])
            .values(
                schema_version=_TARGET_SCHEMA_VERSION,
                compressed_payload=compressed,
                payload_codec=_CODEC,
                uncompressed_size=len(raw),
                record_checksum=_record_checksum(row, compressed, len(raw)),
            )
        )

    with op.batch_alter_table("campaign_snapshots") as batch:
        batch.alter_column("compressed_payload", existing_type=sa.LargeBinary(), nullable=False)
        batch.alter_column("payload_codec", existing_type=sa.String(32), nullable=False)
        batch.alter_column("uncompressed_size", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("record_checksum", existing_type=sa.String(64), nullable=False)
        batch.drop_column("payload")


def downgrade() -> None:
    raise RuntimeError(
        "snapshot compression is an irreversible protocol cutover; restore the pre-migration backup"
    )
