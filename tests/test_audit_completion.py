import pytest
from sqlalchemy import select, text

from sagasmith_core.campaigns import CampaignService
from sagasmith_core.continuity_commit import ContinuityCommitService
from sagasmith_core.models import Campaign, RuleChunk, RuleSource, VectorIndexJob
from sagasmith_core.prepared_imports import PreparedImport
from sagasmith_core.rules import RuleService
from sagasmith_core.snapshots import SnapshotService
from sagasmith_core.state import StateMutationService
from sagasmith_core.timeline import DecisionBase
from sagasmith_core.vector_jobs import VectorIndexJobService


def test_contract_import_does_not_load_storage_or_parsers():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import sagasmith_core.contracts; "
            "assert 'sqlalchemy' not in sys.modules; "
            "assert 'sagasmith_core.modules' not in sys.modules; "
            "assert 'sagasmith_core.parsing' not in sys.modules",
        ],
        check=True,
    )


def test_noop_persists_response_without_mutating_story(database):
    from sagasmith_core.contracts import CommandContext, NoOp
    from sagasmith_core.execution import CommandExecutor

    campaign = CampaignService(database).create(system_id="test", name="NoOp")
    with database.unit_of_work() as work:
        base = DecisionBase.capture(database, work, campaign.id)
    executor = CommandExecutor(database, authorize=lambda work, context: None)
    context = CommandContext(base, "dm", "test.noop", "noop")
    result = executor.execute(context, {}, lambda work: NoOp({"changed": False}))
    assert result.status == "noop"
    assert CampaignService(database).get(campaign.id).revision == base.campaign_revision
    assert executor.execute(context, {}, lambda work: pytest.fail("reapplied")).status == "replayed"

    def invalid_noop(work):
        work.session.execute(text("UPDATE campaigns SET name = 'Wrong'"))
        return NoOp({"changed": False})

    with pytest.raises(ValueError, match="noop handler"):
        executor.execute(CommandContext(base, "dm", "test.noop", "invalid"), {}, invalid_noop)
    assert CampaignService(database).get(campaign.id).name == "NoOp"


def test_command_identity_replays_without_reapplying_and_rejects_old_checkout(database):
    from sagasmith_core.execution import CommandContext, CommandExecutor

    campaign = CampaignService(database).create(system_id="test", name="Commands")
    saved = SnapshotService(database).create(campaign.id)
    with database.unit_of_work() as work:
        base = DecisionBase.capture(database, work, campaign.id)
    context = CommandContext(base, "user:dm", "test.command", "one")
    calls = []

    def apply(work):
        calls.append(1)
        StateMutationService(database).replace_in_work(
            work,
            campaign.id,
            campaign_state={"updated": True},
            expected_campaign_revision=base.campaign_revision,
            operation="test.command",
        )
        return {"answer": [1, 2, 3]}

    allowed = [True]

    def authorize(work, context):
        if not allowed[0]:
            raise PermissionError("revoked")

    executor = CommandExecutor(database, authorize=authorize)
    assert executor.execute(context, {"input": 1}, apply).status == "applied"
    replay = executor.execute(context, {"input": 1}, apply)
    assert replay.status == "replayed" and replay.response == {"answer": [1, 2, 3]}
    assert calls == [1]
    allowed[0] = False
    with pytest.raises(PermissionError, match="revoked"):
        executor.execute(context, {"input": 1}, apply)
    allowed[0] = True
    with pytest.raises(ValueError):
        executor.execute(context, {"input": 2}, apply)
    SnapshotService(database).restore(campaign.id, saved.slot)
    with pytest.raises(ValueError, match="timeline"):
        executor.execute(context, {"input": 1}, apply)


def test_constraint_upgrade_preserves_real_history(tmp_path):
    from sagasmith_core.database import Database, sqlite_database_url
    from sagasmith_core.migrations.schema_20260718_13 import Campaign as HistoricalCampaign

    db = Database(sqlite_database_url(tmp_path / "historical.db"))
    try:
        db.upgrade_schema("20260915_34")
        with db.transaction() as session:
            session.add(
                HistoricalCampaign(
                    id="old",
                    system_id="test",
                    slug="old",
                    name="Old",
                    state={"history": "preserved"},
                )
            )
        db.upgrade_schema()
        with db.transaction() as session:
            row = session.get(Campaign, "old")
            assert row.state == {"history": "preserved"}
            assert row.timeline_epoch == 0
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        db.dispose()


def test_explicit_service_failure_cannot_be_swallowed(database):
    campaign = CampaignService(database).create(system_id="test", name="Before")
    with pytest.raises(RuntimeError, match="rollback-only"):
        with database.unit_of_work() as work:
            work.session.execute(text("UPDATE campaigns SET name = 'Partial'"))
            with pytest.raises(ValueError):
                StateMutationService(database).replace_in_work(work, campaign.id)
    assert CampaignService(database).get(campaign.id).name == "Before"
    with pytest.raises(RuntimeError, match="closed"):
        StateMutationService(database).replace_in_work(work, campaign.id, campaign_state={})


def test_continuity_uses_explicit_service_contracts(database, monkeypatch):
    campaign = CampaignService(database).create(system_id="test", name="Explicit")
    with database.unit_of_work() as work:

        def forbidden(*args, **kwargs):
            raise AssertionError("nested transaction creation is forbidden")

        monkeypatch.setattr(database, "transaction", forbidden)
        result = ContinuityCommitService(database).commit_in_work(
            work,
            campaign.id,
            event={"summary": "The door opens"},
            campaign_state={"door": "open"},
            snapshot={"label": "After"},
        )
        assert result["snapshot"] is not None


def test_restore_invalidates_decision_base(database):
    campaign = CampaignService(database).create(system_id="test", name="Epoch")
    saved = SnapshotService(database).create(campaign.id)
    with database.unit_of_work() as work:
        before = DecisionBase.capture(database, work, campaign.id)
    SnapshotService(database).restore(campaign.id, saved.slot)
    with database.unit_of_work() as work:
        after = DecisionBase.capture(database, work, campaign.id)
    assert after.timeline_epoch == before.timeline_epoch + 1
    with pytest.raises(ValueError, match="stale"):
        with database.unit_of_work() as work:
            before.require_current(database, work)


def test_prepared_import_does_not_share_mutable_inputs():
    document = {"sections": [{"title": "Original"}]}
    vectors = {0: [[1.0, 0.0]]}
    prepared = PreparedImport.build(document, vectors, "model@1")
    document["sections"][0]["title"] = "Changed"
    vectors[0][0][0] = 5.0
    assert prepared.document["sections"][0]["title"] == "Original"
    assert prepared.embeddings[0][0] == (1.0, 0.0)
    with pytest.raises(TypeError):
        prepared.document["sections"][0]["title"] = "Forbidden"


class Embedder:
    model_name = "test@1"
    profile = None

    def encode(self, texts):
        return [[1.0, 0.0] for _ in texts]


def test_preparation_failure_publishes_nothing(database):
    class BadEmbedder(Embedder):
        def encode(self, texts):
            return []

    with pytest.raises(ValueError, match="count"):
        RuleService(database).ingest(
            system_id="test",
            source_key="bad",
            title="Bad",
            content="# Title\nBody",
            embedder=BadEmbedder(),
        )
    with database.transaction() as session:
        assert session.scalar(select(RuleSource)) is None


def test_last_attempt_crash_has_a_terminal_state(database):
    RuleService(database).ingest(
        system_id="test",
        source_key="one",
        title="One",
        content="# Title\nBody",
        embedder=Embedder(),
    )
    with database.transaction() as session:
        job = session.scalar(select(VectorIndexJob))
        job.status, job.attempts = "delivering", 5
        job.lease_token, job.lease_until = "crashed", 0
    result = VectorIndexJobService(database).flush(
        None, system_id="test", collection="rules", embedding_model="test@1"
    )
    assert result.attempted == 0
    with database.transaction() as session:
        assert session.scalar(select(VectorIndexJob)).status == "permanent_failure"


@pytest.mark.parametrize("change", ["model", "content", "vector"])
def test_outbox_rejects_changed_entity(database, change):
    RuleService(database).ingest(
        system_id="test",
        source_key="one",
        title="One",
        content="# Title\nBody",
        embedder=Embedder(),
    )
    with database.transaction() as session:
        row = session.scalar(select(RuleChunk))
        if change == "model":
            row.embedding_model = "another@2"
        elif change == "content":
            row.content = "Different"
        else:
            row.embedding_json = [0.0, 1.0]

    class Store:
        def upsert(self, *args, **kwargs):
            raise AssertionError("stale index data must not be delivered")

    worker = VectorIndexJobService(database)
    assert not worker.status(system_id="test", collection="rules")["ready"]
    result = worker.flush(Store(), system_id="test", collection="rules", embedding_model="test@1")
    assert result.completed == 0
    with database.transaction() as session:
        assert session.scalar(select(VectorIndexJob)).status == "permanent_failure"


def test_extension_restore_is_atomic_and_requires_adapter(database):
    class Extension:
        id = "test-extension"
        system_id = "test"
        schema_version = 1
        fail = False

        def capture(self, session, campaign_id):
            return {"value": session.scalar(text("SELECT value FROM extra_state"))}

        def validate(self, state):
            assert isinstance(state.get("value"), str)

        def clear(self, session, campaign_id):
            session.execute(text("DELETE FROM extra_state"))

        def restore(self, session, campaign_id, state):
            session.execute(text("INSERT INTO extra_state(value) VALUES (:value)"), state)
            if self.fail:
                raise ValueError("extension restore failed")

        def migrate(self, state, from_version):
            return state

    extension = Extension()
    database.state_extensions = (extension,)
    with database.transaction() as session:
        session.execute(text("CREATE TABLE extra_state(value TEXT NOT NULL)"))
        session.execute(text("INSERT INTO extra_state VALUES ('Original')"))
    campaign = CampaignService(database).create(system_id="test", name="Extension")
    snapshots = SnapshotService(database)
    saved = snapshots.create(campaign.id)
    with database.transaction() as session:
        session.execute(text("UPDATE extra_state SET value = 'Current'"))
    extension.fail = True
    with pytest.raises(ValueError, match="extension restore failed"):
        snapshots.restore(campaign.id, saved.slot)
    with database.transaction() as session:
        assert session.scalar(text("SELECT value FROM extra_state")) == "Current"
        assert session.get(Campaign, campaign.id).timeline_epoch == 0
    extension.fail = False
    snapshots.restore(campaign.id, saved.slot)
    with database.transaction() as session:
        assert session.scalar(text("SELECT value FROM extra_state")) == "Original"
    database.state_extensions = ()
    with pytest.raises(ValueError, match="unavailable state extensions"):
        snapshots.restore(campaign.id, saved.slot)
