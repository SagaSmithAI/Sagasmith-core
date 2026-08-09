"""Index per-chunk heading paths in module full-text search."""

from alembic import op

revision = "20260722_14"
down_revision = "20260718_13"
branch_labels = None
depends_on = None


def _exists(name: str, kind: str) -> bool:
    row = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type = ?",
            (name, kind),
        )
        .first()
    )
    return row is not None


def _insert_sql(prefix: str) -> str:
    return (
        "INSERT INTO module_fts(chunk_id, module_title, chapter_title, scene_title, "
        "headings, keywords, tags, scene_type, chunk_type, content) "
        f"SELECT COALESCE({prefix}.id, ''), COALESCE(msrc.title, ''), "
        "COALESCE(mch.title, ''), COALESCE(msc.title, ''), "
        f"COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each({prefix}.heading_path)), ''), "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.keywords)), ''), "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM "
        "json_each(json_extract(msc.metadata_json, '$.tags'))), ''), "
        f"COALESCE(msc.scene_type, ''), COALESCE({prefix}.chunk_type, ''), "
        f"COALESCE({prefix}.content, '') FROM module_scenes msc "
        "JOIN module_chapters mch ON mch.id = msc.chapter_id "
        "JOIN module_sources msrc ON msrc.id = msc.module_id "
        f"WHERE msc.id = {prefix}.scene_id"
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite" or not (
        _exists("module_chunks", "table") and _exists("module_fts", "table")
    ):
        return

    for suffix in ("ai", "ad", "au"):
        bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS module_fts_{suffix}")
    bind.exec_driver_sql(
        "CREATE TRIGGER module_fts_ai AFTER INSERT ON module_chunks BEGIN "
        f"{_insert_sql('new')}; END"
    )
    bind.exec_driver_sql(
        "CREATE TRIGGER module_fts_ad AFTER DELETE ON module_chunks BEGIN "
        "DELETE FROM module_fts WHERE chunk_id = old.id; END"
    )
    bind.exec_driver_sql(
        "CREATE TRIGGER module_fts_au AFTER UPDATE ON module_chunks BEGIN "
        "DELETE FROM module_fts WHERE chunk_id = old.id; "
        f"{_insert_sql('new')}; END"
    )

    bind.exec_driver_sql("DELETE FROM module_fts")
    bind.exec_driver_sql(
        "INSERT INTO module_fts(chunk_id, module_title, chapter_title, scene_title, "
        "headings, keywords, tags, scene_type, chunk_type, content) "
        "SELECT COALESCE(mc.id, ''), COALESCE(msrc.title, ''), "
        "COALESCE(mch.title, ''), COALESCE(msc.title, ''), "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(mc.heading_path)), ''), "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM json_each(msc.keywords)), ''), "
        "COALESCE((SELECT GROUP_CONCAT(value, ' ') FROM "
        "json_each(json_extract(msc.metadata_json, '$.tags'))), ''), "
        "COALESCE(msc.scene_type, ''), COALESCE(mc.chunk_type, ''), "
        "COALESCE(mc.content, '') FROM module_chunks mc "
        "JOIN module_scenes msc ON msc.id = mc.scene_id "
        "JOIN module_chapters mch ON mch.id = msc.chapter_id "
        "JOIN module_sources msrc ON msrc.id = msc.module_id"
    )


def downgrade() -> None:
    # Retain the more precise and safe search triggers. Reverting would silently
    # make room identifiers in chunk heading paths undiscoverable again.
    pass
