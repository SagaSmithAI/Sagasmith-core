import asyncio
import time

import pytest
from sqlalchemy import select, text

from sagasmith_core.access import AccessService
from sagasmith_core.branches import BranchService
from sagasmith_core.campaigns import CampaignService
from sagasmith_core.characters import CharacterService
from sagasmith_core.idempotency import IdempotencyService, IdempotencyWrite
from sagasmith_core.models import ActorGrant, Campaign
from sagasmith_core.snapshots import SnapshotService
from sagasmith_core.state import StateMutationService


def test_nonce_claims_survive_store_recreation_and_expire(database):
    from sagasmith_core.nonce_store import DatabaseNonceStore

    first = DatabaseNonceStore(database)
    now = time.time()
    first.claim("issuer:nonce", now + 60, now, 10)
    with pytest.raises(ValueError, match="already used"):
        DatabaseNonceStore(database).claim("issuer:nonce", now + 60, now, 10)
    DatabaseNonceStore(database).claim("issuer:nonce", now + 120, now + 61, 10)


def test_vector_lease_blocks_another_worker_and_recovers(database):
    from sagasmith_core.models import RuleChunk, VectorIndexJob
    from sagasmith_core.rules import RuleService
    from sagasmith_core.vector_jobs import VectorIndexJobService

    RuleService(database).ingest(
        system_id="test", source_key="source", title="Source", content="# Rule\nA test rule."
    )
    with database.transaction() as session:
        chunk = session.scalar(select(RuleChunk))
        chunk.embedding_json = [1.0, 0.0]
        chunk.embedding_model = "test@1"
        session.add(
            VectorIndexJob(
                id="job",
                system_id="test",
                collection="rules",
                entity_type="rule_chunk",
                entity_id=chunk.id,
                status="delivering",
                attempts=1,
                lease_token="dead-worker",
                lease_until=time.time() + 60,
                payload={"embedding_model": "test@1"},
            )
        )
    calls = []

    class Store:
        def upsert(self, *args, **kwargs):
            database.require_independent_work()
            calls.append(kwargs)

    worker = VectorIndexJobService(database)
    options = {"system_id": "test", "collection": "rules", "embedding_model": "test@1"}
    assert worker.flush(Store(), **options).attempted == 0
    with database.transaction() as session:
        session.get(VectorIndexJob, "job").lease_until = 0
    assert worker.flush(Store(), **options).completed == 1
    assert len(calls) == 1


def test_caught_nested_failure_rolls_back_the_entire_unit(database):
    with pytest.raises(RuntimeError, match="rollback-only"):
        with database.unit_of_work() as work:
            CampaignService(database).create(system_id="test", name="Outer")
            try:
                with database.transaction() as session:
                    session.execute(text("UPDATE campaigns SET name = 'Failed inner'"))
                    raise ValueError("business failure")
            except ValueError:
                pass
    with database.transaction() as session:
        assert list(session.scalars(select(Campaign))) == []
    with pytest.raises(RuntimeError, match="closed"):
        work.require_owner()


def test_explicit_savepoint_allows_local_failure(database):
    with database.unit_of_work() as work:
        campaign = CampaignService(database).create(system_id="test", name="Outer")
        with pytest.raises(ValueError):
            with database.savepoint(work):
                with database.transaction() as session:
                    session.execute(text("UPDATE campaigns SET name = 'Inner'"))
                    raise ValueError("local failure")
    assert CampaignService(database).get(campaign.id).name == "Outer"


def test_child_task_cannot_inherit_a_session(database):
    async def child():
        with pytest.raises(RuntimeError, match="another execution owner"):
            with database.transaction():
                pass

    async def parent():
        with database.unit_of_work():
            await asyncio.create_task(child())

    asyncio.run(parent())


def test_unaudited_idempotency_is_rejected_before_write(database):
    campaign = CampaignService(database).create(system_id="test", name="Before")
    with pytest.raises(ValueError, match="audited"):
        StateMutationService(database).replace(
            campaign.id,
            campaign_state={"bad": True},
            idempotency_key="key",
            idempotency_write=IdempotencyWrite("scope", {}, {}),
        )
    assert CampaignService(database).get(campaign.id).state == campaign.state


def test_receipt_without_mutation_has_an_explicit_branch(database):
    campaign = CampaignService(database).create(system_id="test", name="Receipts")
    service = IdempotencyService(database)
    service.remember("opaque-scope", "key", {}, {"secret": 1}, campaign_id=campaign.id)
    assert (
        service.receipt(campaign.id, "key").branch_id
        == BranchService(database).current(campaign.id).id
    )
    with pytest.raises(LookupError):
        service.receipt(campaign.id, "key", branch_id="unrelated-branch")


def test_restore_never_revives_revoked_private_access_or_stale_revision(database):
    campaign = CampaignService(database).create(system_id="test", name="Restore")
    characters = CharacterService(database)
    actor = characters.create(system_id="test", campaign_id=campaign.id, name="Actor", sheet={})
    access = AccessService(database)
    access.ensure_principal("player")
    access.grant_campaign(campaign.id, "player", role="player")
    with database.transaction() as session:
        session.add(
            ActorGrant(
                campaign_id=campaign.id,
                actor_id=actor.id,
                principal_id="player",
                can_view_private=True,
            )
        )
    snapshots = SnapshotService(database)
    saved = snapshots.create(campaign.id)
    with database.transaction() as session:
        grant = session.scalar(select(ActorGrant))
        grant.can_view_private = False
    characters.update(actor.id, sheet={"changed": True})
    before = characters.get(actor.id).revision
    snapshots.restore(campaign.id, saved.slot)
    assert characters.get(actor.id).revision > before
    with database.transaction() as session:
        assert session.scalar(select(ActorGrant)).can_view_private is False


def test_frozen_migration_chain_reconstructs_current_columns(tmp_path):
    from sqlalchemy import inspect

    from sagasmith_core.database import Database, sqlite_database_url
    from sagasmith_core.models import Base

    database = Database(sqlite_database_url(tmp_path / "history.db"))
    try:
        database.upgrade_schema("20260701_01")
        assert "actor_grants" not in inspect(database.engine).get_table_names()
        database.upgrade_schema()
        inspector = inspect(database.engine)
        for table in Base.metadata.sorted_tables:
            assert {column["name"] for column in inspector.get_columns(table.name)} == set(
                table.columns.keys()
            ), table.name
    finally:
        database.dispose()


def test_fts_filters_before_top_k_and_embedding_runs_before_write(database):
    from sagasmith_core.rules import RuleService

    class Embedder:
        model_name = "test"
        profile = None

        def encode(self, texts):
            database.require_independent_work()
            return [[1.0, 0.0] for _ in texts]

    rules = RuleService(database)
    for index in range(25):
        rules.ingest(
            system_id="test",
            source_key=f"excluded-{index}",
            title="Grapple",
            content="# Grapple\nGrapple grapple grapple.",
            edition="other",
        )
    source = rules.ingest(
        system_id="test",
        source_key="allowed",
        title="Allowed",
        content="# Grapple\nA valid scoped result.",
        edition="2014",
        embedder=Embedder(),
    )
    hits = rules.search(system_id="test", query="grapple", edition="2014", top_k=1)
    assert hits[0].source_id == source.source_id
    with database.transaction():
        with pytest.raises(RuntimeError, match="external work"):
            rules.ingest(
                system_id="test",
                source_key="invalid",
                title="Invalid",
                content="# Test\nNever embed under a writer.",
                embedder=Embedder(),
            )
