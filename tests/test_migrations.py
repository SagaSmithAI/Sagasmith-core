from pathlib import Path

from alembic import command
from sqlalchemy import inspect

from sagasmith_core.database import Database, alembic_config, sqlite_database_url


def test_bundled_migration_builds_schema(tmp_path: Path) -> None:
    database = Database(sqlite_database_url(tmp_path / "migrated.db"))
    database.upgrade_schema()
    try:
        inspector = inspect(database.engine)
        assert "campaigns" in inspector.get_table_names()
        assert "alembic_version" in inspector.get_table_names()
        assert "scope_id" in {
            column["name"] for column in inspector.get_columns("scene_progress")
        }
        assert "redoable" in {
            column["name"] for column in inspector.get_columns("state_revisions")
        }
        assert "template_id" in {
            column["name"] for column in inspector.get_columns("characters")
        }
    finally:
        database.dispose()


def test_scoped_progress_migrates_existing_sqlite_schema(tmp_path: Path) -> None:
    database = Database(sqlite_database_url(tmp_path / "legacy.db"))
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE scene_progress (
                id VARCHAR(36) PRIMARY KEY,
                campaign_id VARCHAR(36) NOT NULL,
                scene_id VARCHAR(36) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'current',
                progress INTEGER NOT NULL DEFAULT 0,
                current_room VARCHAR(500),
                state_version INTEGER NOT NULL DEFAULT 1,
                state JSON NOT NULL DEFAULT '{}',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_scene_progress UNIQUE (campaign_id, scene_id)
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO scene_progress (id, campaign_id, scene_id)
            VALUES ('progress-1', 'campaign-1', 'scene-1')
            """
        )
    config = alembic_config(database.url)
    command.stamp(config, "20260701_02")
    database.upgrade_schema()
    try:
        inspector = inspect(database.engine)
        columns = {column["name"] for column in inspector.get_columns("scene_progress")}
        constraints = inspector.get_unique_constraints("scene_progress")
        with database.engine.connect() as connection:
            scope = connection.exec_driver_sql(
                "SELECT scope_id FROM scene_progress WHERE id = 'progress-1'"
            ).scalar_one()
        assert "scope_id" in columns
        assert scope == "party"
        assert any(
            constraint["column_names"] == ["campaign_id", "scope_id", "scene_id"]
            for constraint in constraints
        )
    finally:
        database.dispose()


def test_snapshot_v2_migrates_existing_revision_history(tmp_path: Path) -> None:
    database = Database(sqlite_database_url(tmp_path / "revision-history.db"))
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE state_revisions (
                id VARCHAR(36) PRIMARY KEY,
                campaign_id VARCHAR(36) NOT NULL,
                sequence INTEGER NOT NULL,
                applied BOOLEAN NOT NULL DEFAULT 1
            )
            """
        )
    config = alembic_config(database.url)
    command.stamp(config, "20260706_04")
    database.upgrade_schema()
    try:
        columns = {
            column["name"] for column in inspect(database.engine).get_columns("state_revisions")
        }
        assert "redoable" in columns
    finally:
        database.dispose()


def test_character_template_migrates_existing_character_library(tmp_path: Path) -> None:
    database = Database(sqlite_database_url(tmp_path / "character-library.db"))
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE characters (
                id VARCHAR(36) PRIMARY KEY,
                system_id VARCHAR(64) NOT NULL,
                campaign_id VARCHAR(36),
                character_type VARCHAR(32) NOT NULL,
                name VARCHAR(200) NOT NULL
            )
            """
        )
    config = alembic_config(database.url)
    command.stamp(config, "20260712_05")
    database.upgrade_schema()
    try:
        columns = {column["name"] for column in inspect(database.engine).get_columns("characters")}
        assert "template_id" in columns
    finally:
        database.dispose()
