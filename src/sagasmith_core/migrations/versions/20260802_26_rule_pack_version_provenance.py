"""Store provenance on the exact rule-pack version.

Revision ID: 20260802_26
Revises: 20260802_25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260802_26"
down_revision = "20260802_25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("rule_pack_versions"):
        return
    columns = {column["name"] for column in inspector.get_columns("rule_pack_versions")}
    if "provenance" not in columns:
        with op.batch_alter_table("rule_pack_versions") as batch:
            batch.add_column(
                sa.Column(
                    "provenance",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )
    op.execute(
        sa.text(
            "UPDATE rule_pack_versions "
            "SET provenance = COALESCE(("
            "SELECT rule_packs.provenance FROM rule_packs "
            "WHERE rule_packs.id = rule_pack_versions.pack_id"
            "), '{}') "
            "WHERE provenance IS NULL "
            "OR CAST(provenance AS TEXT) IN ('{}', 'null')"
        )
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("rule_pack_versions"):
        return
    columns = {column["name"] for column in inspector.get_columns("rule_pack_versions")}
    if "provenance" not in columns:
        return
    with op.batch_alter_table("rule_pack_versions") as batch:
        batch.drop_column("provenance")
