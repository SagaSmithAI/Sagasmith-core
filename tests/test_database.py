from pathlib import Path

from sqlalchemy import inspect

from sagasmith_core.database import Database, sqlite_database_url


def test_general_schema_contains_domain_tables(tmp_path: Path) -> None:
    database = Database(sqlite_database_url(tmp_path / "base.db"))
    database.create_schema()

    tables = set(inspect(database.engine).get_table_names())

    assert {
        "campaigns",
        "characters",
        "rule_sources",
        "rule_sections",
        "rule_chunks",
        "module_sources",
        "module_chapters",
        "module_scenes",
        "module_chunks",
        "scene_progress",
    } <= tables
