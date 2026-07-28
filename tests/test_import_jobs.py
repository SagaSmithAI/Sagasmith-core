import pytest

from sagasmith_core import (
    CampaignService,
    IdempotencyService,
    IdempotencyWrite,
    ImportJobService,
)
from sagasmith_core.import_jobs import ImportJobError


def test_import_job_persists_inspection_candidate_review_and_result(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Import jobs")
    jobs = ImportJobService(database)
    created = jobs.create(
        campaign_id=campaign.id,
        kind="rulebook",
        artifact="xanathar.pdf",
        artifact_checksum="abc123",
        payload={"edition": "2014"},
    )
    inspected = jobs.record_inspection(
        created.id,
        {"sections": 10, "chunks": 35, "parser_profile": "markdown", "parser_version": "1"},
    )
    assert inspected.state == "inspected"
    extracted = jobs.set_candidates(
        created.id,
        [
            {
                "id": "candidate:fireball",
                "kind": "spell",
                "source_chunk_ids": ["chunk-1"],
            }
        ],
    )
    assert extracted.state == "review_required"
    reviewed = jobs.review_candidates(
        created.id,
        [
            {
                "id": "candidate:fireball",
                "review_status": "accepted",
                "artifact": {"kind": "spell", "card": {"name": "Fireball"}},
            }
        ],
    )
    assert reviewed.state == "reviewed"
    compiled = jobs.record_validation(
        created.id,
        {"draft": {"status": "validated"}},
        state="compiled",
    )
    completed = jobs.record_result(
        created.id,
        {"pack_id": "dnd5e.xgte", "version": "1.0.0"},
        state="installed",
        source_id="source-1",
    )
    assert [
        created.revision,
        inspected.revision,
        extracted.revision,
        reviewed.revision,
        compiled.revision,
        completed.revision,
    ] == [0, 1, 2, 3, 4, 5]
    assert completed.source_id == "source-1"
    assert jobs.list(campaign.id)[0].result["pack_id"] == "dnd5e.xgte"


def test_import_job_rejects_invalid_candidate_review(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Import validation")
    jobs = ImportJobService(database)
    job = jobs.create(campaign_id=campaign.id, kind="module", artifact="module.md")
    with pytest.raises(ImportJobError, match="unsupported import kind"):
        jobs.create(campaign_id=campaign.id, kind="other", artifact="x")
    with pytest.raises(ImportJobError, match="candidate ids must be unique"):
        jobs.set_candidates(job.id, [{"id": "same"}, {"id": "same"}])
    with pytest.raises(ImportJobError, match="invalid import job transition"):
        jobs.record_result(job.id, {}, state="activated")


def test_import_job_update_and_exact_replay_receipt_share_one_transaction(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Atomic import")
    jobs = ImportJobService(database)
    job = jobs.create(campaign_id=campaign.id, kind="module", artifact="module.md")
    payload = {"job_id": job.id, "operation": "inspect"}
    inspected = jobs.record_inspection(
        job.id,
        {"parser_profile": "dnd5e", "parser_version": "1"},
        expected_revision=job.revision,
        idempotency_key="inspect-job",
        idempotency_write=IdempotencyWrite(
            scope=f"import-job:{campaign.id}:{job.id}",
            payload=payload,
            response=lambda result: {
                "job_id": result.id,
                "state": result.state,
                "revision": result.revision,
            },
        ),
    )
    replay = IdempotencyService(database).lookup(
        f"import-job:{campaign.id}:{job.id}",
        "inspect-job",
        payload,
    )
    assert replay is not None
    assert replay.response == {
        "job_id": job.id,
        "state": "inspected",
        "revision": inspected.revision,
    }

    def fail_response(_result):
        raise RuntimeError("import response serialization failed")

    with pytest.raises(RuntimeError, match="import response serialization failed"):
        jobs.record_validation(
            job.id,
            {"valid": True},
            expected_revision=inspected.revision,
            idempotency_key="validate-job",
            idempotency_write=IdempotencyWrite(
                scope=f"import-job:{campaign.id}:{job.id}",
                payload={"job_id": job.id, "operation": "validate"},
                response=fail_response,
            ),
        )
    persisted = jobs.get(job.id)
    assert persisted.state == "inspected"
    assert persisted.revision == inspected.revision
