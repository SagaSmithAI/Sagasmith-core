"""Replace rule-pack JSON columns with compressed immutable payloads.

Revision ID: 20260815_31
Revises: 20260815_30
"""

from __future__ import annotations

import hashlib
import json
import zlib
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260815_31"
down_revision = "20260815_30"
branch_labels = None
depends_on = None

_CODEC = "zlib-1"
_LEGACY_COLUMNS = {
    "manifest",
    "artifacts",
    "mechanics",
    "provenance",
    "validation_report",
}


def _json_value(value: Any, expected: type, field: str) -> Any:
    if isinstance(value, str):
        value = json.loads(value)
    if value is None:
        value = expected()
    if not isinstance(value, expected):
        raise RuntimeError(f"rule-pack {field} has an invalid JSON shape")
    return value


def _canonical_payload(value: dict[str, Any]) -> tuple[str, bytes, int]:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), zlib.compress(raw), len(raw)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("rule_pack_versions"):
        return
    columns = {
        str(column["name"])
        for column in inspector.get_columns("rule_pack_versions")
    }
    has_current = "payload_document_id" in columns
    legacy_present = _LEGACY_COLUMNS.intersection(columns)
    if has_current and legacy_present.issubset({"provenance"}):
        if not inspector.has_table("rule_pack_payloads"):
            raise RuntimeError("rule-pack payload references require rule_pack_payloads")
        # Fresh databases are built from current metadata by revision 01.  The
        # historical provenance migration may then add its retired column.
        if "provenance" in legacy_present:
            with op.batch_alter_table("rule_pack_versions") as batch:
                batch.drop_column("provenance")
        return
    if has_current or legacy_present != _LEGACY_COLUMNS:
        raise RuntimeError("rule_pack_versions has an incomplete payload schema")

    if inspector.has_table("rule_pack_payloads"):
        payload_columns = {
            str(column["name"])
            for column in inspector.get_columns("rule_pack_payloads")
        }
        if payload_columns != {
            "id",
            "payload_codec",
            "uncompressed_size",
            "compressed_payload",
            "created_at",
        }:
            raise RuntimeError("rule_pack_payloads has an unsupported pre-migration schema")
        if bind.execute(sa.text("SELECT count(*) FROM rule_pack_payloads")).scalar_one():
            raise RuntimeError("pre-migration rule_pack_payloads must be empty")
    else:
        op.create_table(
            "rule_pack_payloads",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("payload_codec", sa.String(length=32), nullable=False),
            sa.Column("uncompressed_size", sa.Integer(), nullable=False),
            sa.Column("compressed_payload", sa.LargeBinary(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    op.add_column(
        "rule_pack_versions",
        sa.Column("payload_document_id", sa.String(length=64), nullable=True),
    )
    metadata = sa.MetaData()
    versions = sa.Table("rule_pack_versions", metadata, autoload_with=bind)
    payloads = sa.Table("rule_pack_payloads", metadata, autoload_with=bind)
    documents: dict[str, dict[str, Any]] = {}
    updates: list[dict[str, str]] = []
    for row in bind.execute(
        sa.select(
            versions.c.pack_id,
            versions.c.version,
            versions.c.manifest,
            versions.c.artifacts,
            versions.c.mechanics,
            versions.c.provenance,
            versions.c.validation_report,
        )
    ).mappings():
        value = {
            "manifest": _json_value(row["manifest"], dict, "manifest"),
            "artifacts": _json_value(row["artifacts"], list, "artifacts"),
            "mechanics": _json_value(row["mechanics"], list, "mechanics"),
            "provenance": _json_value(row["provenance"], dict, "provenance"),
            "validation_report": _json_value(
                row["validation_report"], dict, "validation_report"
            ),
        }
        document_id, compressed_payload, uncompressed_size = _canonical_payload(value)
        documents.setdefault(
            document_id,
            {
                "id": document_id,
                "payload_codec": _CODEC,
                "uncompressed_size": uncompressed_size,
                "compressed_payload": compressed_payload,
                "created_at": datetime.now(timezone.utc),
            },
        )
        updates.append(
            {
                "pack_id": str(row["pack_id"]),
                "version": str(row["version"]),
                "payload_document_id": document_id,
            }
        )

    if documents:
        bind.execute(payloads.insert(), list(documents.values()))
    if updates:
        bind.execute(
            sa.text(
                "UPDATE rule_pack_versions "
                "SET payload_document_id=:payload_document_id "
                "WHERE pack_id=:pack_id AND version=:version"
            ),
            updates,
        )

    with op.batch_alter_table("rule_pack_versions") as batch:
        batch.create_foreign_key(
            "fk_rule_pack_versions_payload_document_id_rule_pack_payloads",
            "rule_pack_payloads",
            ["payload_document_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_rule_pack_versions_payload_document_id",
            ["payload_document_id"],
        )
        batch.alter_column("payload_document_id", existing_type=sa.String(64), nullable=False)
        for column in sorted(_LEGACY_COLUMNS):
            batch.drop_column(column)


def downgrade() -> None:
    raise RuntimeError("compressed rule-pack payloads are the only supported protocol")
