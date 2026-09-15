"""Converge historical SQLite constraints with the declared storage contract."""

import sqlalchemy as sa
from alembic import op

revision = "20260915_36"
down_revision = "20260915_35"
branch_labels = None
depends_on = None

REFERENCES = {
    "campaigns": [("active_branch_id", "campaign_branches")],
    "characters": [("template_id", "characters")],
    "rule_sources": [("canonical_source_id", "rule_sources")],
    "campaign_events": [
        ("branch_id", "campaign_branches"),
        ("committed_snapshot_id", "campaign_snapshots"),
    ],
    "state_revisions": [("mutation_group_id", "mutation_groups")],
    "campaign_snapshots": [("branch_id", "campaign_branches")],
}


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    triggers = []
    if bind.dialect.name == "sqlite":
        triggers = list(
            bind.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND sql IS NOT NULL"
            )
        )
        for name, _ in triggers:
            bind.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
    try:
        for table, references in REFERENCES.items():
            if table not in tables:
                continue
            inspector = sa.inspect(bind)
            columns = {c["name"]: c for c in inspector.get_columns(table)}
            existing = {tuple(f["constrained_columns"]) for f in inspector.get_foreign_keys(table)}
            missing = [
                (column, target)
                for column, target in references
                if column in columns and target in tables and (column,) not in existing
            ]
            nullable = [
                name
                for name in (
                    "compressed_payload",
                    "payload_codec",
                    "uncompressed_size",
                    "record_checksum",
                )
                if table == "campaign_snapshots" and name in columns and columns[name]["nullable"]
            ]
            if not missing and not nullable:
                continue
            for column, target in missing:
                orphan = bind.exec_driver_sql(
                    f'SELECT 1 FROM "{table}" s LEFT JOIN "{target}" t '
                    f'ON s."{column}" = t.id WHERE s."{column}" IS NOT NULL '
                    "AND t.id IS NULL LIMIT 1"
                ).first()
                if orphan:
                    raise RuntimeError(f"repair orphaned {table}.{column} before upgrading")
            with op.batch_alter_table(table) as batch:
                for column, target in missing:
                    batch.create_foreign_key(
                        f"fk_{table}_{column}", target, [column], ["id"], ondelete="SET NULL"
                    )
                for column in nullable:
                    batch.alter_column(
                        column, existing_type=columns[column]["type"], nullable=False
                    )
    finally:
        for _, sql in triggers:
            bind.exec_driver_sql(sql)


def downgrade():
    raise RuntimeError("restore a matching database backup to downgrade storage contracts")
