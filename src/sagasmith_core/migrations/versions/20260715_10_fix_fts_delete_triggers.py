"""Use normal DELETE statements for normal-content FTS5 tables."""

from alembic import op

revision = "20260715_10"
down_revision = "20260715_09"
branch_labels = None
depends_on = None


def _exists(name: str, kind: str) -> bool:
    row = op.get_bind().exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type = ?",
        (name, kind),
    ).first()
    return row is not None


def _replace(
    name: str,
    table: str,
    fts_table: str,
    update_insert: str,
) -> None:
    if not (_exists(table, "table") and _exists(fts_table, "table")):
        return
    op.get_bind().exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}_ad")
    op.get_bind().exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}_au")
    delete_sql = f"DELETE FROM {fts_table} WHERE chunk_id = old.id"
    op.get_bind().exec_driver_sql(
        f"CREATE TRIGGER {name}_ad AFTER DELETE ON {table} "
        f"BEGIN {delete_sql}; END"
    )
    op.get_bind().exec_driver_sql(
        f"CREATE TRIGGER {name}_au AFTER UPDATE ON {table} BEGIN "
        f"{delete_sql}; {update_insert}; END"
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    _replace(
        "module_fts",
        "module_chunks",
        "module_fts",
        "INSERT INTO module_fts(chunk_id, module_title, chapter_title, scene_title, "
        "headings, keywords, tags, scene_type, chunk_type, content) "
        "SELECT COALESCE(new.id, ''), COALESCE(msrc.title, ''), "
        "COALESCE(mch.title, ''), COALESCE(msc.title, ''), "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.headings)), ''), "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.keywords)), ''), "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM "
        "json_each(json_extract(msc.metadata_json, '$.tags'))), ''), "
        "COALESCE(msc.scene_type, ''), COALESCE(new.chunk_type, ''), "
        "COALESCE(new.content, '') FROM module_scenes msc "
        "JOIN module_chapters mch ON mch.id = msc.chapter_id "
        "JOIN module_sources msrc ON msrc.id = msc.module_id "
        "WHERE msc.id = new.scene_id",
    )
    _replace(
        "rule_fts",
        "rule_chunks",
        "rule_fts",
        "INSERT INTO rule_fts(chunk_id, source_title, section_title, heading_path, content) "
        "SELECT COALESCE(new.id, ''), COALESCE(rsrc.title, ''), "
        "COALESCE(rsec.title, ''), COALESCE((SELECT GROUP_CONCAT(value, ' ') "
        "FROM json_each(new.heading_path)), ''), COALESCE(new.content, '') "
        "FROM rule_sections rsec JOIN rule_sources rsrc ON rsrc.id = rsec.source_id "
        "WHERE rsec.id = new.section_id",
    )


def downgrade() -> None:
    # Retain the safe triggers; restoring the invalid delete form would corrupt
    # ordinary source replacement and chunk updates.
    pass
