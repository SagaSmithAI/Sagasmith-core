import json
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
        assert "revision" in {
            column["name"] for column in inspector.get_columns("import_jobs")
        }
        assert any(
            constraint["name"] == "uq_mutation_group_branch_idempotency"
            and constraint["column_names"]
            == ["campaign_id", "branch_id", "idempotency_key"]
            for constraint in inspector.get_unique_constraints("mutation_groups")
        )
        assert "event_sequence" in {
            column["name"] for column in inspector.get_columns("campaigns")
        }
        assert "campaign_event_participants" in inspector.get_table_names()
        assert {"event_id", "actor_id", "role"} == {
            column["name"]
            for column in inspector.get_columns("campaign_event_participants")
        }
        memory_columns = {
            column["name"] for column in inspector.get_columns("campaign_memories")
        }
        revision_columns = {
            column["name"] for column in inspector.get_columns("memory_revisions")
        }
        rule_source_columns = {
            column["name"] for column in inspector.get_columns("rule_sources")
        }
        snapshot_columns = {
            column["name"] for column in inspector.get_columns("campaign_snapshots")
        }
        branch_columns = {
            column["name"] for column in inspector.get_columns("campaign_branches")
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
        assert "active" not in revision_columns
        assert "active" in rule_source_columns
        assert "is_head" not in snapshot_columns
        assert "is_current" not in branch_columns
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
            "('revision-1', 'memory-1', 'Locked', '{}', 0, CURRENT_TIMESTAMP)"
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
            revision_columns = {
                column["name"]
                for column in inspect(database.engine).get_columns("memory_revisions")
            }
        assert tuple(row) == ("legacy:memory-1", "", "")
        assert tuple(revision) == ("retracted", "[]", 3, "dm")
        assert "active" not in revision_columns
    finally:
        database.dispose()


def test_rule_profile_authority_removes_legacy_campaign_setting_copies(
    tmp_path: Path,
) -> None:
    database = Database(sqlite_database_url(tmp_path / "profile-authority.db"))
    config = alembic_config(database.url)
    command.upgrade(config, "20260728_18")
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO campaigns "
            "(id, system_id, slug, name, status, description, settings, state, revision, "
            "created_at, updated_at) VALUES "
            "('campaign-1', 'dnd5e', 'authority', 'Authority', 'active', '', "
            "'{\"edition\":\"2014\",\"locale\":\"zh\",\"table_name\":\"Friday\"}', "
            "'{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO campaign_rule_profiles "
            "(campaign_id, system_id, edition, locale, publications, options, "
            "created_at, updated_at) VALUES "
            "('campaign-1', 'dnd5e', '2014', 'zh', '[]', '{}', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    database.upgrade_schema()

    try:
        with database.engine.connect() as connection:
            settings = connection.exec_driver_sql(
                "SELECT settings FROM campaigns WHERE id = 'campaign-1'"
            ).scalar_one()
        if isinstance(settings, str):
            settings = json.loads(settings)
        assert settings == {"table_name": "Friday"}
    finally:
        database.dispose()


def test_rule_source_revision_migration_preserves_sqlite_fts_triggers(
    tmp_path: Path,
) -> None:
    database = Database(sqlite_database_url(tmp_path / "rule-source-revisions.db"))
    config = alembic_config(database.url)
    command.upgrade(config, "20260728_19")
    with database.engine.begin() as connection:
        if "active" in {
            column["name"]
            for column in inspect(connection).get_columns("rule_sources")
        }:
            connection.exec_driver_sql("ALTER TABLE rule_sources DROP COLUMN active")

    database.upgrade_schema()

    try:
        inspector = inspect(database.engine)
        assert "active" in {
            column["name"] for column in inspector.get_columns("rule_sources")
        }
        with database.engine.begin() as connection:
            trigger_names = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND name LIKE 'rule_fts_%'"
                )
            }
            connection.exec_driver_sql(
                "INSERT INTO rule_sources "
                "(id, system_id, source_key, title, locale, edition, version, "
                "publication_id, authority, checksum, active, metadata_json, "
                "created_at, updated_at) VALUES "
                "('source-1', 'dnd5e', 'legacy-rules', 'Legacy Rules', 'en', "
                "'2014', '', '', 'primary', 'checksum', 1, '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.exec_driver_sql(
                "INSERT INTO rule_sections "
                "(id, source_id, ordinal, level, title, path, content, "
                "start_offset, end_offset) VALUES "
                "('section-1', 'source-1', 0, 1, 'Combat', '[\"Combat\"]', "
                "'', 0, 0)"
            )
            connection.exec_driver_sql(
                "INSERT INTO rule_chunks "
                "(id, source_id, section_id, ordinal, heading_path, content, "
                "token_count, metadata_json) VALUES "
                "('chunk-1', 'source-1', 'section-1', 0, '[\"Combat\"]', "
                "'migration sentinel', 2, '{}')"
            )
            indexed = connection.exec_driver_sql(
                "SELECT content FROM rule_fts WHERE chunk_id = 'chunk-1'"
            ).scalar_one()
        assert trigger_names == {"rule_fts_ai", "rule_fts_ad", "rule_fts_au"}
        assert indexed == "migration sentinel"
    finally:
        database.dispose()


def test_rule_source_revision_migration_recovers_interrupted_sqlite_batch(
    tmp_path: Path,
) -> None:
    database = Database(sqlite_database_url(tmp_path / "interrupted-rule-source.db"))
    config = alembic_config(database.url)
    command.upgrade(config, "20260728_19")
    with database.engine.begin() as connection:
        if "active" in {
            column["name"]
            for column in inspect(connection).get_columns("rule_sources")
        }:
            connection.exec_driver_sql("ALTER TABLE rule_sources DROP COLUMN active")
        connection.exec_driver_sql(
            "CREATE TABLE _alembic_tmp_rule_sources (id VARCHAR(36) PRIMARY KEY)"
        )

    database.upgrade_schema()

    try:
        inspector = inspect(database.engine)
        assert "_alembic_tmp_rule_sources" not in inspector.get_table_names()
        assert "active" in {
            column["name"] for column in inspector.get_columns("rule_sources")
        }
    finally:
        database.dispose()
