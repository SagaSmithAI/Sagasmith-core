import pytest
from sqlalchemy import select

from sagasmith_core.campaigns import CampaignService
from sagasmith_core.models import VectorIndexJob
from sagasmith_core.modules import ModuleService
from sagasmith_core.rules import RuleService
from sagasmith_core.vector_jobs import VectorIndexJobService


class FakeEmbedder:
    model_name = "fake"
    dimensions = 2
    model_id = "embedding-fake"
    profile = None

    def encode(self, texts):
        return [[1.0, 0.0] if "grapple" in text.casefold() else [0.0, 1.0] for text in texts]


class RevisionedFakeEmbedder(FakeEmbedder):
    def __init__(self, embedding_model_id: str) -> None:
        self.embedding_model_id = embedding_model_id


class FakeVectorStore:
    enabled = True

    def __init__(self) -> None:
        self.fail_upsert = False
        self.upserts: list[dict] = []
        self.queries: list[dict] = []

    def upsert(
        self,
        name,
        *,
        ids,
        embeddings,
        metadatas=None,
        documents=None,
        profile=None,
    ) -> None:
        call = {
            "name": name,
            "ids": list(ids),
            "embeddings": list(embeddings),
            "metadatas": list(metadatas or []),
            "documents": list(documents or []),
            "profile": profile,
        }
        self.upserts.append(call)
        if self.fail_upsert:
            raise RuntimeError("vector backend unavailable")

    def query(
        self,
        name,
        *,
        query_embedding,
        limit=20,
        where=None,
        profile=None,
    ):
        self.queries.append(
            {
                "name": name,
                "query_embedding": list(query_embedding),
                "limit": limit,
                "where": where,
                "profile": profile,
            }
        )
        indexed_ids = [item_id for call in self.upserts for item_id in call["ids"]]
        return [(item_id, 1.0) for item_id in indexed_ids]


def vector_jobs(database) -> list[VectorIndexJob]:
    with database.transaction() as session:
        return list(
            session.scalars(
                select(VectorIndexJob).order_by(
                    VectorIndexJob.created_at,
                    VectorIndexJob.id,
                )
            )
        )


def test_rule_vectors_are_delivered_only_after_sql_commit(database) -> None:
    store = FakeVectorStore()
    service = RuleService(database)

    result = service.ingest(
        system_id="dnd5e",
        source_key="atomic-vector-rules",
        title="Atomic Vector Rules",
        content="# Combat\nCore combat.\n## Grapple\nUse an ability check.",
        embedder=FakeEmbedder(),
        vector_store=store,
    )

    assert result.embeddings == 2
    assert store.upserts == []
    assert {job.status for job in vector_jobs(database)} == {"pending"}

    hits = service.search(
        system_id="dnd5e",
        query="grapple",
        embedder=FakeEmbedder(),
        vector_store=store,
    )

    assert hits
    assert len(store.upserts) == 1
    assert len(store.queries) == 1
    assert {job.status for job in vector_jobs(database)} == {"completed"}
    assert {job.attempts for job in vector_jobs(database)} == {1}


def test_module_vectors_are_delivered_only_after_sql_commit(database) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Atomic module vectors",
    )
    store = FakeVectorStore()
    service = ModuleService(database)

    result = service.ingest(
        campaign_id=campaign.id,
        source_key="atomic-vector-module",
        title="Atomic Vector Module",
        content="# Chapter\n## Ambush\nThe goblins prepare an ambush.",
        embedder=FakeEmbedder(),
        vector_store=store,
    )

    assert result.embeddings == 1
    assert store.upserts == []
    assert {job.status for job in vector_jobs(database)} == {"pending"}

    hits = service.search(
        campaign_id=campaign.id,
        query="ambush",
        embedder=FakeEmbedder(),
        vector_store=store,
    )

    assert hits
    assert len(store.upserts) == 1
    assert len(store.queries) == 1
    assert store.upserts[0]["name"] == "modules"
    assert {job.status for job in vector_jobs(database)} == {"completed"}


def test_failed_vector_delivery_is_retry_safe(database) -> None:
    store = FakeVectorStore()
    RuleService(database).ingest(
        system_id="dnd5e",
        source_key="retry-vector-rules",
        title="Retry Vector Rules",
        content="# Grapple\nUse an ability check.",
        embedder=FakeEmbedder(),
        vector_store=store,
    )
    service = VectorIndexJobService(database)
    store.fail_upsert = True

    failed = service.flush(
        store,
        system_id="dnd5e",
        collection="rules",
        embedding_model="fake",
    )

    jobs_after_failure = vector_jobs(database)
    assert failed.attempted == 1
    assert failed.failed == 1
    assert jobs_after_failure[0].status == "failed"
    assert jobs_after_failure[0].attempts == 1
    stable_ids = store.upserts[0]["ids"]

    store.fail_upsert = False
    completed = service.flush(
        store,
        system_id="dnd5e",
        collection="rules",
        embedding_model="fake",
    )

    jobs_after_retry = vector_jobs(database)
    assert completed.completed == 1
    assert jobs_after_retry[0].status == "completed"
    assert jobs_after_retry[0].attempts == 2
    assert store.upserts[1]["ids"] == stable_ids


def test_vector_flush_never_crosses_embedding_revisions(database) -> None:
    store = FakeVectorStore()
    service = RuleService(database)
    service.ingest(
        system_id="dnd5e",
        source_key="revision-one",
        title="Revision one",
        content="# First\nOriginal revision.",
        embedder=RevisionedFakeEmbedder("fake@" + "1" * 40),
        vector_store=store,
    )
    service.ingest(
        system_id="dnd5e",
        source_key="revision-two",
        title="Revision two",
        content="# Second\nReplacement revision.",
        embedder=RevisionedFakeEmbedder("fake@" + "2" * 40),
        vector_store=store,
    )

    result = VectorIndexJobService(database).flush(
        store,
        system_id="dnd5e",
        collection="rules",
        embedding_model="fake@" + "2" * 40,
    )

    jobs = vector_jobs(database)
    assert (result.attempted, result.completed, result.failed) == (1, 1, 0)
    assert len(store.upserts) == 1
    assert {
        (job.payload["embedding_model"], job.status)
        for job in jobs
    } == {
        ("fake@" + "1" * 40, "pending"),
        ("fake@" + "2" * 40, "completed"),
    }


def test_vector_flush_rejects_a_mismatched_profile_revision(database) -> None:
    class Profile:
        storage_model_id = "fake@" + "1" * 40

    with pytest.raises(ValueError, match="profile does not match"):
        VectorIndexJobService(database).flush(
            FakeVectorStore(),
            system_id="dnd5e",
            collection="rules",
            embedding_model="fake@" + "2" * 40,
            profile=Profile(),
        )
