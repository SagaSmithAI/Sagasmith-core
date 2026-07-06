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
