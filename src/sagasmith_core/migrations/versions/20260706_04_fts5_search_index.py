"""Add FTS5 full-text search with per-column BM25 weights.

FTS5 is a SQLite feature — this migration only runs on SQLite backends.
PostgreSQL users rely on the existing structured_score + enrich_query path.

Multi-column layout enables ``bm25(table, w0, w1, …)`` per-column weight
decay, keeping the signal from short metadata fields (scene_title,
chapter_title) much higher than long content fields.

Column layout
-------------
module_fts (10 columns):
  0 chunk_id UNINDEXED (weight 0, no searchable content)
  1 module_title       (weight 8.0)
  2 chapter_title      (weight 6.0)
  3 scene_title        (weight 4.0)
  4 headings           (weight 3.0)
  5 keywords           (weight 2.5)
  6 tags               (weight 2.0)
  7 scene_type         (weight 2.0)
  8 chunk_type         (weight 1.5)
  9 content            (weight 1.0)

rule_fts (5 columns):
  0 chunk_id UNINDEXED (weight 0)
  1 source_title       (weight 5.0)
  2 section_title      (weight 5.0)
  3 heading_path       (weight 3.0)
  4 content            (weight 1.0)
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


_MODULE_FTS_COLS = (
    "chunk_id UNINDEXED",
    "module_title",
    "chapter_title",
    "scene_title",
    "headings",
    "keywords",
    "tags",
    "scene_type",
    "chunk_type",
    "content",
)

_RULE_FTS_COLS = (
    "chunk_id UNINDEXED",
    "source_title",
    "section_title",
    "heading_path",
    "content",
)


def _cols_ddl(cols: tuple[str, ...]) -> str:
    """Column list for CREATE VIRTUAL TABLE (preserves UNINDEXED)."""
    return ", ".join(cols)


def _cols_dml(cols: tuple[str, ...]) -> str:
    """Column list for INSERT/SELECT (strips UNINDEXED qualifier)."""
    return ", ".join(c.split()[0] for c in cols)


def _module_select(prefix: str) -> list[str]:
    """Column expressions for a module_fts INSERT … SELECT.

    *prefix* is the alias prefix for the module_chunks row: ``"new"``
    inside triggers, ``"mc"`` for bulk rebuild.
    """
    p = prefix
    return [
        f"COALESCE({p}.id, '')",
        "COALESCE(msrc.title, '')",
        "COALESCE(mch.title, '')",
        "COALESCE(msc.title, '')",
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.headings)), '')",
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.keywords)), '')",
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(json_extract(msc.metadata_json, '$.tags'))), '')",
        "COALESCE(msc.scene_type, '')",
        f"COALESCE({p}.chunk_type, '')",
        f"COALESCE({p}.content, '')",
    ]


def _module_join_suffix() -> str:
    return (
        "JOIN module_chapters mch ON mch.id = msc.chapter_id "
        "JOIN module_sources msrc ON msrc.id = msc.module_id "
    )


def _module_trigger_where(p: str) -> str:
    return f"WHERE msc.id = {p}.scene_id"


def _module_rebuild_join(p: str) -> str:
    return (
        f"JOIN module_scenes msc ON msc.id = {p}.scene_id "
        f"JOIN module_chapters mch ON mch.id = msc.chapter_id "
        f"JOIN module_sources msrc ON msrc.id = msc.module_id"
    )


def _rule_select(p: str) -> list[str]:
    return [
        f"COALESCE({p}.id, '')",
        "COALESCE(rsrc.title, '')",
        "COALESCE(rsec.title, '')",
        f"COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each({p}.heading_path)), '')",
        f"COALESCE({p}.content, '')",
    ]


def _rule_join_suffix() -> str:
    return "JOIN rule_sources rsrc ON rsrc.id = rsec.source_id "


def _rule_trigger_where(p: str) -> str:
    return f"WHERE rsec.id = {p}.section_id"


def _rule_rebuild_join(p: str) -> str:
    return (
        f"JOIN rule_sections rsec ON rsec.id = {p}.section_id "
        f"JOIN rule_sources rsrc ON rsrc.id = rsec.source_id"
    )


_MODULE_TRIGGERS = [
    (
        "module_fts_ai",
        "AFTER INSERT ON module_chunks",
        f"INSERT INTO module_fts({_cols_dml(_MODULE_FTS_COLS)}) "
        f"SELECT {', '.join(_module_select('new'))} "
        f"FROM module_scenes msc {_module_join_suffix()}{_module_trigger_where('new')}",
    ),
    (
        "module_fts_ad",
        "AFTER DELETE ON module_chunks",
        "INSERT INTO module_fts(module_fts, chunk_id) VALUES('delete', old.id)",
    ),
    (
        "module_fts_au",
        "AFTER UPDATE ON module_chunks",
        "INSERT INTO module_fts(module_fts, chunk_id) VALUES('delete', old.id); "
        f"INSERT INTO module_fts({_cols_dml(_MODULE_FTS_COLS)}) "
        f"SELECT {', '.join(_module_select('new'))} "
        f"FROM module_scenes msc {_module_join_suffix()}{_module_trigger_where('new')}",
    ),
]

_RULE_TRIGGERS = [
    (
        "rule_fts_ai",
        "AFTER INSERT ON rule_chunks",
        f"INSERT INTO rule_fts({_cols_dml(_RULE_FTS_COLS)}) "
        f"SELECT {', '.join(_rule_select('new'))} "
        f"FROM rule_sections rsec {_rule_join_suffix()}{_rule_trigger_where('new')}",
    ),
    (
        "rule_fts_ad",
        "AFTER DELETE ON rule_chunks",
        "INSERT INTO rule_fts(rule_fts, chunk_id) VALUES('delete', old.id)",
    ),
    (
        "rule_fts_au",
        "AFTER UPDATE ON rule_chunks",
        "INSERT INTO rule_fts(rule_fts, chunk_id) VALUES('delete', old.id); "
        f"INSERT INTO rule_fts({_cols_dml(_RULE_FTS_COLS)}) "
        f"SELECT {', '.join(_rule_select('new'))} "
        f"FROM rule_sections rsec {_rule_join_suffix()}{_rule_trigger_where('new')}",
    ),
]


def upgrade() -> None:
    if not _is_sqlite():
        return

    tables = set(sa.inspect(op.get_bind()).get_table_names())

    # ── Module search FTS ──────────────────────────────────────────
    op.get_bind().exec_driver_sql(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS module_fts "
        f"USING fts5({_cols_ddl(_MODULE_FTS_COLS)}, tokenize='unicode61 remove_diacritics 1')"
    )

    if "module_chunks" in tables:
        existing = op.get_bind().exec_driver_sql(
            "SELECT COUNT(*) FROM module_chunks"
        ).scalar()
        if existing and existing > 0:
            op.get_bind().exec_driver_sql(
                f"INSERT INTO module_fts({_cols_dml(_MODULE_FTS_COLS)}) "
                f"SELECT {', '.join(_module_select('mc'))} "
                f"FROM module_chunks mc "
                f"{_module_rebuild_join('mc')}"
            )

        for name, event, body in _MODULE_TRIGGERS:
            op.get_bind().exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {name} {event} BEGIN {body}; END"
            )

    # ── Rule search FTS ────────────────────────────────────────────
    op.get_bind().exec_driver_sql(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS rule_fts "
        f"USING fts5({_cols_ddl(_RULE_FTS_COLS)}, tokenize='unicode61 remove_diacritics 1')"
    )

    if "rule_chunks" in tables:
        existing = op.get_bind().exec_driver_sql(
            "SELECT COUNT(*) FROM rule_chunks"
        ).scalar()
        if existing and existing > 0:
            op.get_bind().exec_driver_sql(
                f"INSERT INTO rule_fts({_cols_dml(_RULE_FTS_COLS)}) "
                f"SELECT {', '.join(_rule_select('rc'))} "
                f"FROM rule_chunks rc "
                f"{_rule_rebuild_join('rc')}"
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
