"""Add stable identities and lifecycle fields to campaign facts."""

import sqlalchemy as sa
from alembic import op

revision = "20260723_15"
down_revision = "20260722_14"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _columns(table: str) -> dict[str, dict]:
    return {item["name"]: item for item in sa.inspect(op.get_bind()).get_columns(table)}


def _unique_constraints(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_unique_constraints(table)
        if item.get("name")
    }


def _indexes(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_indexes(table)
        if item.get("name")
    }


def upgrade() -> None:
    campaign_columns = _columns("campaigns") if _has_table("campaigns") else {}
    if campaign_columns and "event_sequence" not in campaign_columns:
        with op.batch_alter_table("campaigns") as batch:
            batch.add_column(
                sa.Column("event_sequence", sa.Integer(), nullable=False, server_default="0")
            )
        if _has_table("campaign_events"):
            op.execute(
                "UPDATE campaigns SET event_sequence = COALESCE(("
                "SELECT MAX(sequence) FROM campaign_events "
                "WHERE campaign_events.campaign_id = campaigns.id), 0)"
            )

    if not _has_table("campaign_memories") or not _has_table("memory_revisions"):
        return
    memory_columns = _columns("campaign_memories")
    if not {"fact_key", "subject_ref", "predicate"}.issubset(memory_columns):
        with op.batch_alter_table("campaign_memories") as batch:
            if "fact_key" not in memory_columns:
                batch.add_column(sa.Column("fact_key", sa.String(length=300), nullable=True))
            if "subject_ref" not in memory_columns:
                batch.add_column(
                    sa.Column(
                        "subject_ref", sa.String(length=300), nullable=False, server_default=""
                    )
                )
            if "predicate" not in memory_columns:
                batch.add_column(
                    sa.Column(
                        "predicate", sa.String(length=200), nullable=False, server_default=""
                    )
                )
    op.execute("UPDATE campaign_memories SET fact_key = 'legacy:' || id WHERE fact_key IS NULL")
    memory_columns = _columns("campaign_memories")
    needs_not_null = bool(memory_columns["fact_key"].get("nullable", True))
    needs_unique = "uq_campaign_memory_fact_key" not in _unique_constraints(
        "campaign_memories"
    )
    needs_index = "ix_campaign_memory_subject_ref" not in _indexes("campaign_memories")
    if needs_not_null or needs_unique or needs_index:
        with op.batch_alter_table("campaign_memories") as batch:
            if needs_not_null:
                batch.alter_column(
                    "fact_key", existing_type=sa.String(length=300), nullable=False
                )
            if needs_unique:
                batch.create_unique_constraint(
                    "uq_campaign_memory_fact_key", ["campaign_id", "fact_key"]
                )
            if needs_index:
                batch.create_index(
                    "ix_campaign_memory_subject_ref",
                    ["campaign_id", "subject_ref"],
                    unique=False,
                )

    revision_columns = _columns("memory_revisions")
    desired = {
        "status": sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="active"
        ),
        "valid_from": sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        "valid_to": sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        "source_event_ids": sa.Column(
            "source_event_ids", sa.JSON(), nullable=False, server_default="[]"
        ),
        "importance": sa.Column(
            "importance", sa.Integer(), nullable=False, server_default="3"
        ),
        "disclosure_scope": sa.Column(
            "disclosure_scope", sa.String(length=32), nullable=False, server_default="dm"
        ),
    }
    if not set(desired).issubset(revision_columns):
        with op.batch_alter_table("memory_revisions") as batch:
            for name, column in desired.items():
                if name not in revision_columns:
                    batch.add_column(column)


def downgrade() -> None:
    if not _has_table("campaign_memories") or not _has_table("memory_revisions"):
        return
    with op.batch_alter_table("memory_revisions") as batch:
        batch.drop_column("disclosure_scope")
        batch.drop_column("importance")
        batch.drop_column("source_event_ids")
        batch.drop_column("valid_to")
        batch.drop_column("valid_from")
        batch.drop_column("status")
    with op.batch_alter_table("campaign_memories") as batch:
        batch.drop_index("ix_campaign_memory_subject_ref")
        batch.drop_constraint("uq_campaign_memory_fact_key", type_="unique")
        batch.drop_column("predicate")
        batch.drop_column("subject_ref")
        batch.drop_column("fact_key")
    if _has_table("campaigns") and "event_sequence" in _columns("campaigns"):
        with op.batch_alter_table("campaigns") as batch:
            batch.drop_column("event_sequence")
