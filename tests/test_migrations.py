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
        assert "scope_id" in {column["name"] for column in inspector.get_columns("scene_progress")}
        assert "current_location_key" in {
            column["name"] for column in inspector.get_columns("scene_progress")
        }
        assert "redoable" in {column["name"] for column in inspector.get_columns("state_revisions")}
        assert "template_id" in {column["name"] for column in inspector.get_columns("characters")}
        assert "rule_pack_versions" in inspector.get_table_names()
        assert "campaign_rule_activations" in inspector.get_table_names()
        assert "rule_resolution_receipts" in inspector.get_table_names()
        assert any(
            constraint["name"] == "uq_mutation_group_branch_idempotency"
            and constraint["column_names"]
            == ["campaign_id", "branch_id", "idempotency_key"]
            for constraint in inspector.get_unique_constraints("mutation_groups")
        )
        assert "event_sequence" in {
            column["name"] for column in inspector.get_columns("campaigns")
        }
        memory_columns = {
            column["name"] for column in inspector.get_columns("campaign_memories")
        }
        revision_columns = {
            column["name"] for column in inspector.get_columns("memory_revisions")
        }
        assert {"fact_key", "subject_ref", "predicate"}.issubset(memory_columns)
        assert {
            "status",
            "valid_from",
            "valid_to",
            "source_event_ids",
            "importance",
            "disclosure_scope",
        }.issubset(revision_columns)
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
        assert "current_location_key" in columns
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


def test_branch_continuity_does_not_backfill_existing_campaigns(tmp_path: Path) -> None:
    database = Database(sqlite_database_url(tmp_path / "branch-continuity.db"))
    config = alembic_config(database.url)
    command.upgrade(config, "20260712_06")
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO campaigns "
            "(id, system_id, slug, name, status, description, settings, state, revision, "
            "created_at, updated_at) "
            "VALUES ('legacy-campaign', 'dnd5e', 'legacy', 'Legacy campaign', 'active', '', "
            "'{}', '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    database.upgrade_schema()
    try:
        with database.engine.connect() as connection:
            active_branch_id = connection.exec_driver_sql(
                "SELECT active_branch_id FROM campaigns WHERE id = 'legacy-campaign'"
            ).scalar_one()
            branch_count = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM campaign_branches WHERE campaign_id = 'legacy-campaign'"
            ).scalar_one()
        assert active_branch_id is None
        assert branch_count == 0
    finally:
        database.dispose()


def test_long_term_memory_v2_backfills_stable_legacy_fact_keys(tmp_path: Path) -> None:
    database = Database(sqlite_database_url(tmp_path / "legacy-memory.db"))
    config = alembic_config(database.url)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE campaign_memories ("
            "id VARCHAR(36) PRIMARY KEY, campaign_id VARCHAR(36) NOT NULL, "
            "kind VARCHAR(64) NOT NULL DEFAULT 'fact', subject VARCHAR(300) NOT NULL DEFAULT '', "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE memory_revisions ("
            "id VARCHAR(36) PRIMARY KEY, memory_id VARCHAR(36) NOT NULL, "
            "parent_id VARCHAR(36), snapshot_id VARCHAR(36), content TEXT NOT NULL, "
            "metadata_json JSON NOT NULL DEFAULT '{}', active BOOLEAN NOT NULL DEFAULT 1, "
            "created_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO campaign_memories "
            "(id, campaign_id, kind, subject, created_at, updated_at) VALUES "
            "('memory-1', 'campaign-1', 'fact', 'Door', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO memory_revisions "
            "(id, memory_id, content, metadata_json, active, created_at) VALUES "
            "('revision-1', 'memory-1', 'Locked', '{}', 1, CURRENT_TIMESTAMP)"
        )

    command.stamp(config, "20260722_14")
    database.upgrade_schema()

    try:
        with database.engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT fact_key, subject_ref, predicate FROM campaign_memories "
                "WHERE id = 'memory-1'"
            ).one()
            revision = connection.exec_driver_sql(
                "SELECT status, source_event_ids, importance, disclosure_scope "
                "FROM memory_revisions WHERE id = 'revision-1'"
            ).one()
        assert tuple(row) == ("legacy:memory-1", "", "")
        assert tuple(revision) == ("active", "[]", 3, "dm")
    finally:
        database.dispose()
