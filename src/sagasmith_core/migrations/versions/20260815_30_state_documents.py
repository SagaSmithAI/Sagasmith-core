"""Replace revision JSON copies with compressed immutable state documents.

Revision ID: 20260815_30
Revises: 20260814_29
"""

from __future__ import annotations

import hashlib
import json
import zlib
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260815_30"
down_revision = "20260814_29"
branch_labels = None
depends_on = None

_CODEC = "zlib-1"


def _canonical_document(value: Any) -> tuple[str, bytes, int]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError("state revision payload must be a JSON object or null")
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
    if not inspector.has_table("state_revisions"):
        return
    columns = {str(column["name"]) for column in inspector.get_columns("state_revisions")}
    current_columns = {"before_document_id", "after_document_id"}
    if current_columns.issubset(columns) and "before" not in columns and "after" not in columns:
        if not inspector.has_table("state_documents"):
            raise RuntimeError("state revision document references require state_documents")
        return
    has_before = "before" in columns
    has_after = "after" in columns
    if has_before != has_after:
        raise RuntimeError("state_revisions has an incomplete legacy JSON payload schema")
    has_legacy_payloads = has_before and has_after
    if inspector.has_table("state_documents"):
        document_columns = {
            str(column["name"]) for column in inspector.get_columns("state_documents")
        }
        if document_columns != {
            "id",
            "payload_codec",
            "uncompressed_size",
            "compressed_payload",
            "created_at",
        }:
            raise RuntimeError("state_documents has an unsupported pre-migration schema")
        if bind.execute(sa.text("SELECT count(*) FROM state_documents")).scalar_one():
            raise RuntimeError("pre-migration state_documents must be empty")
    else:
        op.create_table(
            "state_documents",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("payload_codec", sa.String(length=32), nullable=False),
            sa.Column("uncompressed_size", sa.Integer(), nullable=False),
            sa.Column("compressed_payload", sa.LargeBinary(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    op.add_column(
        "state_revisions",
        sa.Column("before_document_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "state_revisions",
        sa.Column("after_document_id", sa.String(length=64), nullable=True),
    )

    metadata = sa.MetaData()
    revisions = sa.Table("state_revisions", metadata, autoload_with=bind)
    documents = sa.Table("state_documents", metadata, autoload_with=bind)
    rows = (
        {
            str(item["id"]): dict(item)
            for item in bind.execute(
                sa.select(
                    revisions.c.id,
                    revisions.c.branch_key,
                    revisions.c.before,
                    revisions.c.after,
                )
            ).mappings()
        }
        if has_legacy_payloads
        else {}
    )

    resolved: dict[tuple[str, str], Any] = {}

    def resolve_value(revision_id: str, field: str, visited: set[str] | None = None) -> Any:
        key = (revision_id, field)
        if key in resolved:
            return resolved[key]
        item = rows[revision_id]
        value = item[field]
        if value is not None:
            resolved[key] = value
            return value
        source_id = str(item.get("branch_key") or "")
        chain = set() if visited is None else set(visited)
        if source_id in chain:
            raise RuntimeError("state revision payload source contains a cycle")
        if source_id in rows and source_id != revision_id:
            chain.add(revision_id)
            value = resolve_value(source_id, field, chain)
        resolved[key] = value
        return value

    encoded_documents: dict[str, dict[str, Any]] = {}
    updates: list[dict[str, str | None]] = []
    for revision_id in rows:
        document_ids: dict[str, str | None] = {}
        for field in ("before", "after"):
            value = resolve_value(revision_id, field)
            if value is None:
                document_ids[field] = None
                continue
            document_id, compressed_payload, uncompressed_size = _canonical_document(value)
            document_ids[field] = document_id
            encoded_documents.setdefault(
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
                "revision_id": revision_id,
                "before_document_id": document_ids["before"],
                "after_document_id": document_ids["after"],
            }
        )

    if encoded_documents:
        bind.execute(documents.insert(), list(encoded_documents.values()))
    if updates:
        bind.execute(
            sa.text(
                "UPDATE state_revisions "
                "SET before_document_id=:before_document_id, "
                "after_document_id=:after_document_id "
                "WHERE id=:revision_id"
            ),
            updates,
        )

    with op.batch_alter_table("state_revisions") as batch:
        batch.create_foreign_key(
            "fk_state_revisions_before_document_id_state_documents",
            "state_documents",
            ["before_document_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_state_revisions_after_document_id_state_documents",
            "state_documents",
            ["after_document_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_state_revisions_before_document_id",
            ["before_document_id"],
        )
        batch.create_index(
            "ix_state_revisions_after_document_id",
            ["after_document_id"],
        )
        if has_legacy_payloads:
            batch.drop_column("before")
            batch.drop_column("after")


def downgrade() -> None:
    raise RuntimeError("compressed state documents are the only supported revision protocol")
