"""Preserve immutable rule-source revisions across reimports."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_20"
down_revision = "20260728_19"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("rule_sources"):
        return set()
    return {str(item["name"]) for item in inspector.get_columns("rule_sources")}


def _clean_interrupted_sqlite_batch() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    inspector = sa.inspect(bind)
    if inspector.has_table("_alembic_tmp_rule_sources"):
        bind.exec_driver_sql("DROP TABLE _alembic_tmp_rule_sources")


def _drop_sqlite_rule_fts_triggers() -> bool:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return False
    inspector = sa.inspect(bind)
    if not (
        inspector.has_table("rule_chunks")
        and inspector.has_table("rule_fts")
    ):
        return False
    for suffix in ("ai", "ad", "au"):
        bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS rule_fts_{suffix}")
    return True


def _restore_sqlite_rule_fts_triggers() -> None:
    bind = op.get_bind()
    insert_sql = (
        "INSERT INTO rule_fts("
        "chunk_id, source_title, section_title, heading_path, content"
        ") SELECT COALESCE(new.id, ''), COALESCE(rsrc.title, ''), "
        "COALESCE(rsec.title, ''), "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') "
        "FROM json_each(new.heading_path)), ''), COALESCE(new.content, '') "
        "FROM rule_sections rsec "
        "JOIN rule_sources rsrc ON rsrc.id = rsec.source_id "
        "WHERE rsec.id = new.section_id"
    )
    bind.exec_driver_sql(
        f"CREATE TRIGGER rule_fts_ai AFTER INSERT ON rule_chunks "
        f"BEGIN {insert_sql}; END"
    )
    bind.exec_driver_sql(
        "CREATE TRIGGER rule_fts_ad AFTER DELETE ON rule_chunks "
        "BEGIN DELETE FROM rule_fts WHERE chunk_id = old.id; END"
    )
    bind.exec_driver_sql(
        "CREATE TRIGGER rule_fts_au AFTER UPDATE ON rule_chunks "
        "BEGIN DELETE FROM rule_fts WHERE chunk_id = old.id; "
        f"{insert_sql}; END"
    )


def upgrade() -> None:
    columns = _columns()
    if not columns or "active" in columns:
        return
    _clean_interrupted_sqlite_batch()
    restore_rule_fts = _drop_sqlite_rule_fts_triggers()
    try:
        with op.batch_alter_table("rule_sources") as batch:
            batch.add_column(
                sa.Column(
                    "active",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("1"),
                )
            )
    finally:
        if restore_rule_fts:
            _restore_sqlite_rule_fts_triggers()


def downgrade() -> None:
    columns = _columns()
    if not columns or "active" not in columns:
        return
    _clean_interrupted_sqlite_batch()
    restore_rule_fts = _drop_sqlite_rule_fts_triggers()
    try:
        with op.batch_alter_table("rule_sources") as batch:
            batch.drop_column("active")
    finally:
        if restore_rule_fts:
            _restore_sqlite_rule_fts_triggers()
