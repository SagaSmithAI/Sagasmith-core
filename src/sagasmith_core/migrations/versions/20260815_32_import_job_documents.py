"""Replace import-job JSON columns with one compressed mutable document.

Revision ID: 20260815_32
Revises: 20260815_31
"""

from __future__ import annotations

import hashlib
import json
import zlib
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260815_32"
down_revision = "20260815_31"
branch_labels = None
depends_on = None

_CODEC = "zlib-1"
_LEGACY_FIELDS = {
    "payload": dict,
    "inspection": dict,
    "candidates": list,
    "validation": dict,
    "result": dict,
}
_CURRENT_COLUMNS = {
    "document_codec",
    "document_uncompressed_size",
    "document_checksum",
    "compressed_document",
}


def _json_value(value: Any, expected: type, field: str) -> Any:
    if isinstance(value, str):
        value = json.loads(value)
    if value is None:
        value = expected()
    if not isinstance(value, expected):
        raise RuntimeError(f"import job {field} has an invalid JSON shape")
    if field == "candidates" and any(not isinstance(item, dict) for item in value):
        raise RuntimeError("import job candidates must be an array of objects")
    return value


def _encode(value: dict[str, Any]) -> tuple[str, int, bytes]:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw), zlib.compress(raw)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("import_jobs"):
        return
    columns = {str(column["name"]) for column in inspector.get_columns("import_jobs")}
    current_present = _CURRENT_COLUMNS.intersection(columns)
    legacy_present = set(_LEGACY_FIELDS).intersection(columns)
    if current_present == _CURRENT_COLUMNS and not legacy_present:
        return
    if current_present or legacy_present != set(_LEGACY_FIELDS):
        raise RuntimeError("import_jobs has an incomplete document storage schema")

    op.add_column(
        "import_jobs",
        sa.Column("document_codec", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "import_jobs",
        sa.Column("document_uncompressed_size", sa.Integer(), nullable=True),
    )
    op.add_column(
        "import_jobs",
        sa.Column("document_checksum", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "import_jobs",
        sa.Column("compressed_document", sa.LargeBinary(), nullable=True),
    )

    metadata = sa.MetaData()
    jobs = sa.Table("import_jobs", metadata, autoload_with=bind)
    updates: list[dict[str, Any]] = []
    for row in bind.execute(
        sa.select(jobs.c.id, *(jobs.c[field] for field in _LEGACY_FIELDS))
    ).mappings():
        document = {
            field: _json_value(row[field], expected, field)
            for field, expected in _LEGACY_FIELDS.items()
        }
        checksum, size, compressed = _encode(document)
        updates.append(
            {
                "id": str(row["id"]),
                "codec": _CODEC,
                "size": size,
                "checksum": checksum,
                "compressed": compressed,
            }
        )
    if updates:
        bind.execute(
            sa.text(
                "UPDATE import_jobs SET document_codec=:codec, "
                "document_uncompressed_size=:size, document_checksum=:checksum, "
                "compressed_document=:compressed WHERE id=:id"
            ),
            updates,
        )

    with op.batch_alter_table("import_jobs") as batch:
        batch.alter_column("document_codec", existing_type=sa.String(32), nullable=False)
        batch.alter_column(
            "document_uncompressed_size", existing_type=sa.Integer(), nullable=False
        )
        batch.alter_column("document_checksum", existing_type=sa.String(64), nullable=False)
        batch.alter_column("compressed_document", existing_type=sa.LargeBinary(), nullable=False)
        for field in sorted(_LEGACY_FIELDS):
            batch.drop_column(field)


def downgrade() -> None:
    raise RuntimeError("compressed import-job documents are the only supported protocol")
