"""Add state history, rule profiles, document provenance, and index jobs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from sagasmith_core.models import Base

revision = "20260701_02"
down_revision = "20260701_01"
branch_labels = None
depends_on = None


def _add(table: str, column: sa.Column) -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {item["name"] for item in inspector.get_columns(table)}
    if column.name not in existing:
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)

    _add("rule_sources", sa.Column("edition", sa.String(64), nullable=False, server_default=""))
    _add(
        "rule_sources",
        sa.Column("publication_id", sa.String(200), nullable=False, server_default=""),
    )
    _add(
        "rule_sources",
        sa.Column("authority", sa.String(32), nullable=False, server_default="primary"),
    )
    _add("rule_sources", sa.Column("canonical_source_id", sa.String(36), nullable=True))

    _add("module_sources", sa.Column("source_path", sa.Text(), nullable=False, server_default=""))
    _add(
        "module_sources",
        sa.Column("parser_profile", sa.String(100), nullable=False, server_default="generic"),
    )
    _add(
        "module_sources",
        sa.Column("parser_version", sa.String(32), nullable=False, server_default="1"),
    )
    _add("module_sources", sa.Column("warnings", sa.JSON(), nullable=False, server_default="[]"))

    _add("module_chapters", sa.Column("source_path", sa.Text(), nullable=False, server_default=""))
    _add(
        "module_chapters",
        sa.Column("status", sa.String(32), nullable=False, server_default="locked"),
    )
    _add("module_chapters", sa.Column("page_start", sa.Integer(), nullable=True))
    _add("module_chapters", sa.Column("page_end", sa.Integer(), nullable=True))

    _add(
        "module_scenes",
        sa.Column("scene_type", sa.String(32), nullable=False, server_default="section"),
    )
    _add("module_scenes", sa.Column("start_line", sa.Integer(), nullable=False, server_default="1"))
    _add("module_scenes", sa.Column("end_line", sa.Integer(), nullable=False, server_default="1"))
    _add("module_scenes", sa.Column("page_start", sa.Integer(), nullable=True))
    _add("module_scenes", sa.Column("page_end", sa.Integer(), nullable=True))
    _add("module_scenes", sa.Column("headings", sa.JSON(), nullable=False, server_default="[]"))
    _add("module_scenes", sa.Column("keywords", sa.JSON(), nullable=False, server_default="[]"))

    for name, column in (
        ("start_line", sa.Column("start_line", sa.Integer(), nullable=False, server_default="1")),
        ("end_line", sa.Column("end_line", sa.Integer(), nullable=False, server_default="1")),
        ("char_start", sa.Column("char_start", sa.Integer(), nullable=False, server_default="0")),
        ("char_end", sa.Column("char_end", sa.Integer(), nullable=False, server_default="0")),
        ("page_start", sa.Column("page_start", sa.Integer(), nullable=True)),
        ("page_end", sa.Column("page_end", sa.Integer(), nullable=True)),
        (
            "chunk_type",
            sa.Column("chunk_type", sa.String(32), nullable=False, server_default="narrative"),
        ),
        (
            "content_hash",
            sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        ),
    ):
        _add("module_chunks", column)

    _add("scene_progress", sa.Column("current_room", sa.String(500), nullable=True))
    _add(
        "scene_progress",
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    # This migration intentionally keeps user campaign history on downgrade.
    pass
