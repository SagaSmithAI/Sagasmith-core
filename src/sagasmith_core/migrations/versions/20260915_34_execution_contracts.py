"""Explicit receipt ownership and vector delivery leases."""

import sqlalchemy as sa
from alembic import op

revision = "20260915_34"
down_revision = "20260815_33"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("auth_nonce_records"):
        op.create_table(
            "auth_nonce_records",
            sa.Column("key", sa.String(64), primary_key=True),
            sa.Column("expires_at", sa.Float(), nullable=False),
        )
        op.create_index("ix_auth_nonce_records_expires_at", "auth_nonce_records", ["expires_at"])
    for table, columns in {
        "idempotency_records": [
            sa.Column("branch_id", sa.String(36), nullable=True),
            sa.Column("scope_type", sa.String(32), nullable=False, server_default="legacy"),
            sa.Column("identity", sa.JSON(), nullable=False, server_default="{}"),
        ],
        "vector_index_jobs": [
            sa.Column("lease_token", sa.String(36), nullable=True),
            sa.Column("lease_until", sa.Float(), nullable=True),
            sa.Column("next_attempt_at", sa.Float(), nullable=False, server_default="0"),
        ],
    }.items():
        if not sa.inspect(bind).has_table(table):
            continue
        existing = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
    if not sa.inspect(bind).has_table("idempotency_records"):
        return
    bind.execute(
        sa.text(
            "UPDATE idempotency_records SET branch_id = "
            "(SELECT branch_id FROM mutation_groups WHERE id = mutation_group_id), "
            "scope_type = 'branch' WHERE mutation_group_id IS NOT NULL"
        )
    )


def downgrade():
    raise RuntimeError("execution ownership cannot be discarded by downgrade")
