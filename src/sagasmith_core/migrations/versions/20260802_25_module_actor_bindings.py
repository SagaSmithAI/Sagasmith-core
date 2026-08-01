"""Add portable module actor bindings.

Revision ID: 20260802_25
Revises: 20260731_24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260802_25"
down_revision = "20260731_24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("module_actor_bindings"):
        return
    op.create_table(
        "module_actor_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("module_id", sa.String(length=36), nullable=False),
        sa.Column("scene_id", sa.String(length=36), nullable=True),
        sa.Column("scene_key", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("portable_actor_id", sa.String(length=200), nullable=False),
        sa.Column("binding_kind", sa.String(length=50), nullable=False, server_default="cast"),
        sa.Column("role", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["module_id"], ["module_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scene_id"], ["module_scenes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "module_id",
            "scene_key",
            "character_id",
            "binding_kind",
            "role",
            name="uq_module_actor_binding",
        ),
    )
    op.create_index(
        "ix_module_actor_bindings_module_id",
        "module_actor_bindings",
        ["module_id"],
    )
    op.create_index(
        "ix_module_actor_bindings_scene_id",
        "module_actor_bindings",
        ["scene_id"],
    )
    op.create_index(
        "ix_module_actor_bindings_character_id",
        "module_actor_bindings",
        ["character_id"],
    )
    op.create_index(
        "ix_module_actor_binding_scene",
        "module_actor_bindings",
        ["module_id", "scene_key"],
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("module_actor_bindings"):
        return
    op.drop_index("ix_module_actor_binding_scene", table_name="module_actor_bindings")
    op.drop_index("ix_module_actor_bindings_character_id", table_name="module_actor_bindings")
    op.drop_index("ix_module_actor_bindings_scene_id", table_name="module_actor_bindings")
    op.drop_index("ix_module_actor_bindings_module_id", table_name="module_actor_bindings")
    op.drop_table("module_actor_bindings")
