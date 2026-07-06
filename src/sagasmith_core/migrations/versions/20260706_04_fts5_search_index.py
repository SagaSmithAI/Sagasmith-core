"""Add FTS5 full-text search virtual tables for module and rule chunks.

FTS5 is a SQLite feature — this migration only runs on SQLite backends.
PostgreSQL users rely on the existing structured_score + enrich_query path.

The FTS tables store ``chunk_id`` as a stored-but-UNINDEXED column so
the UUID primary key survives round-trip without any integer mapping.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260706_04"
down_revision = "20260704_03"
branch_labels = None
depends_on = None


def _is_sqlite() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "sqlite"


def _build_module_search_text(connection, *, chunk_id: bool = False, del_rowid: bool = False):
    """Raw SQL fragment to build denormalised search text for one chunk row."""
    select_parts = [
        "COALESCE(msrc.title, '') || '  ' || COALESCE(mch.title, '') || '  ' ||"
        "COALESCE(msc.title, '') || '  ' ||"
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.headings)), '') || '  ' ||"
        "COALESCE(msc.scene_type, '') || '  ' ||"
        "COALESCE(mc.chunk_type, '') || '  ' ||"
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.keywords)), '') || '  ' ||"
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(json_extract(msc.metadata_json, '$.tags'))), '') || '  ' ||"
        "COALESCE(mc.content, '')",
    ]
    sql = "SELECT "
    if chunk_id:
        sql += "mc.id, "
    else:
        sql += ""
    sql += select_parts[0]
    sql += " FROM module_chunks mc"
    sql += " JOIN module_scenes msc ON msc.id = mc.scene_id"
    sql += " JOIN module_chapters mch ON mch.id = msc.chapter_id"
    sql += " JOIN module_sources msrc ON msrc.id = mc.module_id"
    return sql


def _build_rule_search_text(connection, *, chunk_id: bool = False):
    select_parts = [
        "COALESCE(rsrc.title, '') || '  ' ||"
        "COALESCE(rsec.title, '') || '  ' ||"
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(rc.heading_path)), '') || '  ' ||"
        "COALESCE(rc.content, '')",
    ]
    sql = "SELECT "
    if chunk_id:
        sql += "rc.id, "
    else:
        sql += ""
    sql += select_parts[0]
    sql += " FROM rule_chunks rc"
    sql += " JOIN rule_sections rsec ON rsec.id = rc.section_id"
    sql += " JOIN rule_sources rsrc ON rsrc.id = rc.source_id"
    return sql


_MODULE_TRIGGERS = [
    (
        "module_fts_ai",
        "AFTER INSERT ON module_chunks",
        "INSERT INTO module_fts(chunk_id, search_text) "
        "SELECT new.id, "
        "COALESCE(msrc.title, '') || '  ' || COALESCE(mch.title, '') || '  ' || "
        "COALESCE(msc.title, '') || '  ' || "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.headings)), '') || '  ' || "
        "COALESCE(msc.scene_type, '') || '  ' || "
        "COALESCE(new.chunk_type, '') || '  ' || "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.keywords)), '') || '  ' || "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(json_extract(msc.metadata_json, '$.tags'))), '') || '  ' || "
        "COALESCE(new.content, '') "
        "FROM module_scenes msc "
        "JOIN module_chapters mch ON mch.id = msc.chapter_id "
        "JOIN module_sources msrc ON msrc.id = msc.module_id "
        "WHERE msc.id = new.scene_id",
    ),
    (
        "module_fts_ad",
        "AFTER DELETE ON module_chunks",
        "INSERT INTO module_fts(module_fts, chunk_id, search_text) "
        "VALUES('delete', old.id, '')",
    ),
    (
        "module_fts_au",
        "AFTER UPDATE ON module_chunks",
        "INSERT INTO module_fts(module_fts, chunk_id, search_text) "
        "VALUES('delete', old.id, ''); "
        "INSERT INTO module_fts(chunk_id, search_text) "
        "SELECT new.id, "
        "COALESCE(msrc.title, '') || '  ' || COALESCE(mch.title, '') || '  ' || "
        "COALESCE(msc.title, '') || '  ' || "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.headings)), '') || '  ' || "
        "COALESCE(msc.scene_type, '') || '  ' || "
        "COALESCE(new.chunk_type, '') || '  ' || "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.keywords)), '') || '  ' || "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(json_extract(msc.metadata_json, '$.tags'))), '') || '  ' || "
        "COALESCE(new.content, '') "
        "FROM module_scenes msc "
        "JOIN module_chapters mch ON mch.id = msc.chapter_id "
        "JOIN module_sources msrc ON msrc.id = msc.module_id "
        "WHERE msc.id = new.scene_id",
    ),
]

_RULE_TRIGGERS = [
    (
        "rule_fts_ai",
        "AFTER INSERT ON rule_chunks",
        "INSERT INTO rule_fts(chunk_id, search_text) "
        "SELECT new.id, "
        "COALESCE(rsrc.title, '') || '  ' || "
        "COALESCE(rsec.title, '') || '  ' || "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(new.heading_path)), '') || '  ' || "
        "COALESCE(new.content, '') "
        "FROM rule_sections rsec "
        "JOIN rule_sources rsrc ON rsrc.id = rsec.source_id "
        "WHERE rsec.id = new.section_id",
    ),
    (
        "rule_fts_ad",
        "AFTER DELETE ON rule_chunks",
        "INSERT INTO rule_fts(rule_fts, chunk_id, search_text) "
        "VALUES('delete', old.id, '')",
    ),
    (
        "rule_fts_au",
        "AFTER UPDATE ON rule_chunks",
        "INSERT INTO rule_fts(rule_fts, chunk_id, search_text) "
        "VALUES('delete', old.id, ''); "
        "INSERT INTO rule_fts(chunk_id, search_text) "
        "SELECT new.id, "
        "COALESCE(rsrc.title, '') || '  ' || "
        "COALESCE(rsec.title, '') || '  ' || "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(new.heading_path)), '') || '  ' || "
        "COALESCE(new.content, '') "
        "FROM rule_sections rsec "
        "JOIN rule_sources rsrc ON rsrc.id = rsec.source_id "
        "WHERE rsec.id = new.section_id",
    ),
]


def upgrade() -> None:
    if not _is_sqlite():
        return

    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    # ── Module search FTS ──────────────────────────────────────────
    op.get_bind().exec_driver_sql(
        "CREATE VIRTUAL TABLE IF NOT EXISTS module_fts "
        "USING fts5(chunk_id UNINDEXED, search_text, "
        "tokenize='unicode61 remove_diacritics 1')"
    )

    if "module_chunks" in tables:
        existing = op.get_bind().exec_driver_sql(
            "SELECT COUNT(*) FROM module_chunks"
        ).scalar()
        if existing and existing > 0:
            op.get_bind().exec_driver_sql(
                f"INSERT INTO module_fts(chunk_id, search_text) "
                f"{_build_module_search_text(op.get_bind(), chunk_id=True)}"
            )

        for name, event, body in _MODULE_TRIGGERS:
            op.get_bind().exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {name} {event} BEGIN {body}; END"
            )

    # ── Rule search FTS ────────────────────────────────────────────
    op.get_bind().exec_driver_sql(
        "CREATE VIRTUAL TABLE IF NOT EXISTS rule_fts "
        "USING fts5(chunk_id UNINDEXED, search_text, "
        "tokenize='unicode61 remove_diacritics 1')"
    )

    if "rule_chunks" in tables:
        existing = op.get_bind().exec_driver_sql(
            "SELECT COUNT(*) FROM rule_chunks"
        ).scalar()
        if existing and existing > 0:
            op.get_bind().exec_driver_sql(
                f"INSERT INTO rule_fts(chunk_id, search_text) "
                f"{_build_rule_search_text(op.get_bind(), chunk_id=True)}"
            )

        for name, event, body in _RULE_TRIGGERS:
            op.get_bind().exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {name} {event} BEGIN {body}; END"
            )


def downgrade() -> None:
    if not _is_sqlite():
        return
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS module_fts")
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS rule_fts")
