import pytest
from sqlalchemy import select

from sagasmith_core import (
    CampaignService,
    IdempotencyService,
    IdempotencyWrite,
    ImportJobService,
)
from sagasmith_core.import_jobs import ImportJobError
from sagasmith_core.models import ImportJob
from sagasmith_core.state_document_storage import StateDocumentStorageError


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
    assert reviewed.state == "review_required"
    assert reviewed.candidates[0]["draft_state"] == "editing"
    finalized = jobs.finalize_candidate_review(
        created.id,
        confirmation={"confirmed_by": "agent:test", "method": "agent"},
    )
    assert finalized.state == "reviewed"
    assert finalized.candidates[0]["draft_state"] == "finalized"
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
        finalized.revision,
        compiled.revision,
        completed.revision,
    ] == [0, 1, 2, 3, 4, 5, 6]
    assert completed.source_id == "source-1"
    assert jobs.list(campaign.id)[0].result["pack_id"] == "dnd5e.xgte"


def test_import_job_document_is_compressed_exact_and_corruption_fails_closed(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Compressed import")
    jobs = ImportJobService(database)
    source_text = "repeatable source evidence\n" * 5000
    created = jobs.create(
        campaign_id=campaign.id,
        kind="module",
        artifact="large-module.pdf",
        payload={"source_text": source_text, "nested": {"enabled": True}},
    )
    inspected = jobs.record_inspection(
        created.id,
        {"parser_profile": "pdf", "parser_version": "2", "pages": list(range(100))},
    )
    assert inspected.payload == {"source_text": source_text, "nested": {"enabled": True}}

    with database.transaction() as session:
        row = session.scalar(select(ImportJob).where(ImportJob.id == created.id))
        assert row is not None
        assert len(row.compressed_document) < row.document_uncompressed_size
        row.compressed_document += b"trailing"

    with pytest.raises(StateDocumentStorageError, match="decompressed size"):
        jobs.get(created.id)


def test_inspected_import_can_rerun_inspection_after_text_review(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="OCR review")
    jobs = ImportJobService(database)
    created = jobs.create(campaign_id=campaign.id, kind="rulebook", artifact="rules.pdf")
    inspected = jobs.record_inspection(created.id, {"parser_profile": "pdf"})

    reviewed = jobs.record_inspection(
        inspected.id,
        {
            "parser_profile": "pdf",
            "page_revisions": [{"page_number": 12}],
        },
        expected_revision=inspected.revision,
    )

    assert reviewed.state == "inspected"
    assert reviewed.revision == inspected.revision + 1
    assert reviewed.inspection["page_revisions"] == [{"page_number": 12}]


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


def test_compiled_import_can_reopen_for_new_evidence_before_install(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Recompile import")
    jobs = ImportJobService(database)
    created = jobs.create(campaign_id=campaign.id, kind="rulebook", artifact="rules.pdf")
    inspected = jobs.record_inspection(created.id, {"parser_profile": "pdf"})
    extracted = jobs.set_candidates(
        inspected.id,
        [{"id": "candidate:old", "review_status": "pending"}],
    )
    reviewed = jobs.review_candidates(
        extracted.id,
        [{"id": "candidate:old", "review_status": "accepted"}],
    )
    finalized = jobs.finalize_candidate_review(
        reviewed.id,
        confirmation={"confirmed_by": "agent:test", "method": "agent"},
    )
    compiled = jobs.record_validation(
        finalized.id,
        {"draft": {"status": "validated", "checksum": "old"}},
        state="compiled",
    )

    reopened = jobs.set_candidates(
        compiled.id,
        [{"id": "candidate:corrected", "review_status": "pending"}],
        expected_revision=compiled.revision,
    )

    assert reopened.state == "review_required"
    assert reopened.validation == {}
    assert [candidate["id"] for candidate in reopened.candidates] == ["candidate:corrected"]


def test_review_required_import_can_add_source_bound_candidates(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Catalog augmentation")
    jobs = ImportJobService(database)
    created = jobs.create(campaign_id=campaign.id, kind="rulebook", artifact="rules.pdf")
    inspected = jobs.record_inspection(created.id, {"parser_profile": "pdf"})
    extracted = jobs.set_candidates(inspected.id, [{"id": "candidate:parsed"}])

    augmented = jobs.set_candidates(
        extracted.id,
        [
            {"id": "candidate:parsed"},
            {"id": "candidate:agent-source-bound"},
        ],
        expected_revision=extracted.revision,
    )

    assert augmented.state == "review_required"
    assert [item["id"] for item in augmented.candidates] == [
        "candidate:parsed",
        "candidate:agent-source-bound",
    ]


def test_candidate_decisions_remain_editable_until_explicit_finalization(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Editable review")
    jobs = ImportJobService(database)
    created = jobs.create(campaign_id=campaign.id, kind="rulebook", artifact="rules.pdf")
    inspected = jobs.record_inspection(created.id, {"parser_profile": "pdf"})
    extracted = jobs.set_candidates(
        inspected.id,
        [{"id": "candidate:one", "artifact": {"kind": "feature"}}],
    )

    accepted = jobs.review_candidates(
        extracted.id,
        [
            {
                "id": "candidate:one",
                "review_status": "accepted",
                "editor": "agent:author",
                "operation": "classify",
                "note": "Initial classification.",
            }
        ],
    )
    reopened = jobs.review_candidates(
        accepted.id,
        [
            {
                "id": "candidate:one",
                "review_status": "pending",
                "editor": "agent:author",
                "operation": "reopen",
                "note": "Evidence needs another pass.",
            }
        ],
    )

    assert accepted.state == reopened.state == "review_required"
    assert reopened.candidates[0]["review_status"] == "pending"
    assert [item["operation"] for item in reopened.candidates[0]["edit_history"]] == [
        "classify",
        "reopen",
    ]
    with pytest.raises(ImportJobError, match="unresolved dispositions"):
        jobs.finalize_candidate_review(
            reopened.id,
            confirmation={"confirmed_by": "agent:author", "method": "agent"},
        )

    rejected = jobs.review_candidates(
        reopened.id,
        [
            {
                "id": "candidate:one",
                "review_status": "rejected",
                "disposition": "exclude",
                "editor": "agent:author",
                "operation": "exclude_fragment",
            }
        ],
    )
    finalized = jobs.finalize_candidate_review(
        rejected.id,
        confirmation={"confirmed_by": "agent:author", "method": "agent"},
    )

    assert finalized.state == "reviewed"
    assert finalized.result["review_finalization"]["candidate_set_fingerprint"]
    with pytest.raises(ImportJobError, match="only an editable"):
        jobs.finalize_candidate_review(
            finalized.id,
            confirmation={"confirmed_by": "agent:author", "method": "agent"},
        )


def test_agent_can_finalize_a_reviewed_empty_candidate_inventory(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Empty review")
    jobs = ImportJobService(database)
    created = jobs.create(campaign_id=campaign.id, kind="rulebook", artifact="rules.md")
    inspected = jobs.record_inspection(created.id, {"parser_profile": "markdown"})
    extracted = jobs.set_candidates(inspected.id, [])

    finalized = jobs.finalize_candidate_review(
        extracted.id,
        confirmation={"confirmed_by": "agent:author", "method": "agent"},
    )

    assert finalized.state == "reviewed"
    assert finalized.candidates == []
    assert finalized.result["review_finalization"]["candidate_set_fingerprint"]


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
