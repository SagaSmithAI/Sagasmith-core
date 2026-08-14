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

    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"

    database.dispose()
