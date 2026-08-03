"""Add portable addon library and branch-local activation locks.

Revision ID: 20260802_27
Revises: 20260802_26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260802_27"
down_revision = "20260802_26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("content_addons"):
        op.create_table(
            "content_addons",
            sa.Column("id", sa.String(length=200), nullable=False),
            sa.Column("system_id", sa.String(length=64), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_content_addons_system_id", "content_addons", ["system_id"]
        )
    if not inspector.has_table("content_addon_versions"):
        op.create_table(
            "content_addon_versions",
            sa.Column("addon_id", sa.String(length=200), nullable=False),
            sa.Column("version", sa.String(length=64), nullable=False),
            sa.Column("manifest", sa.JSON(), nullable=False),
            sa.Column("components", sa.JSON(), nullable=False),
            sa.Column("package", sa.JSON(), nullable=False),
            sa.Column("provenance", sa.JSON(), nullable=False),
            sa.Column("checksum", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("validation_report", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["addon_id"], ["content_addons.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("addon_id", "version"),
        )
        op.create_index(
            "ix_content_addon_versions_checksum",
            "content_addon_versions",
            ["checksum"],
        )
        op.create_index(
            "ix_content_addon_versions_status",
            "content_addon_versions",
            ["status"],
        )
    if not inspector.has_table("campaign_addon_activations"):
        op.create_table(
            "campaign_addon_activations",
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("branch_id", sa.String(length=36), nullable=False),
            sa.Column("addon_id", sa.String(length=200), nullable=False),
            sa.Column("version", sa.String(length=64), nullable=False),
            sa.Column("checksum", sa.String(length=64), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("component_locks", sa.JSON(), nullable=False),
            sa.Column("options", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["addon_id"], ["content_addons.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["branch_id"], ["campaign_branches.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["campaign_id"], ["campaigns.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("campaign_id", "branch_id", "addon_id"),
            sa.UniqueConstraint(
                "campaign_id",
                "branch_id",
                "addon_id",
                name="uq_campaign_branch_addon",
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("campaign_addon_activations"):
        op.drop_table("campaign_addon_activations")
    if inspector.has_table("content_addon_versions"):
        op.drop_index(
            "ix_content_addon_versions_status",
            table_name="content_addon_versions",
        )
        op.drop_index(
            "ix_content_addon_versions_checksum",
            table_name="content_addon_versions",
        )
        op.drop_table("content_addon_versions")
    if inspector.has_table("content_addons"):
        op.drop_index("ix_content_addons_system_id", table_name="content_addons")
        op.drop_table("content_addons")
