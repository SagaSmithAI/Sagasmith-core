import pytest

from sagasmith_core import IdempotencyService, IdempotencyWrite
from sagasmith_core.campaigns import CampaignService
from sagasmith_core.documents import NormalizedDocument
from sagasmith_core.rules import RuleService


class FakeEmbedder:
    model_name = "fake"
    dimensions = 2
    model_id = "embedding-fake"

    def encode(self, texts):
        return [[1.0, 0.0] if "grapple" in text.casefold() else [0.0, 1.0] for text in texts]


def test_rule_ingest_is_incremental_and_searchable(database) -> None:
    service = RuleService(database)
    content = "# Combat\nCore combat.\n## Grapple\nA grapple uses an ability check."

    first = service.ingest(
        system_id="dnd5e",
        source_key="srd",
        title="SRD",
        content=content,
        embedder=FakeEmbedder(),
    )
    second = service.ingest(
        system_id="dnd5e",
        source_key="srd",
        title="SRD",
        content=content,
    )
    hits = service.search(
        system_id="dnd5e",
        query="grapple",
        embedder=FakeEmbedder(),
    )

    assert first.chunks == 2
    assert first.embeddings == 2
    assert second.skipped is True
    assert hits[0].title == "Grapple"
    assert service.expand(hits[0].id)["source"]["key"] == "srd"


def test_rule_search_can_be_bound_to_exact_sources(database) -> None:
    service = RuleService(database)
    first = service.ingest(
        system_id="dnd5e",
        source_key="book-a",
        title="Book A",
        content="# Shared\nFirst source procedure.",
        publication_id="a",
    )
    service.ingest(
        system_id="dnd5e",
        source_key="book-b",
        title="Book B",
        content="# Shared\nSecond source procedure.",
        publication_id="b",
    )

    by_id = service.search(system_id="dnd5e", query="Shared", source_ids=[first.source_id])
    by_key = service.search(system_id="dnd5e", query="Shared", source_keys=["book-b"])

    assert {hit.source_id for hit in by_id} == {first.source_id}
    assert {hit.metadata["source_key"] for hit in by_key} == {"book-b"}


def test_rule_reimport_retires_old_revision_without_breaking_exact_citations(
    database,
) -> None:
    service = RuleService(database)
    old = service.ingest(
        system_id="dnd5e",
        source_key="srd",
        title="SRD",
        content="# Grapple\nOld procedure.",
    )
    old_hit = service.search(
        system_id="dnd5e",
        query="Old procedure",
        source_ids=[old.source_id],
    )[0]

    current = service.ingest(
        system_id="dnd5e",
        source_key="srd",
        title="SRD",
        content="# Grapple\nCurrent procedure.",
    )

    default_hits = service.search(system_id="dnd5e", query="Old procedure")
    assert {hit.source_id for hit in default_hits} == {current.source_id}
    current_hits = service.search(system_id="dnd5e", query="Current procedure")
    assert {hit.source_id for hit in current_hits} == {current.source_id}
    old_citation = service.citation(old_hit.id, source_id=old.source_id)
    assert old_citation["source_key"] == "srd"
    assert service.expand(old_hit.id)["source"]["key"] == "srd"
    sources = service.sources(system_id="dnd5e", include_retired=True)
    assert {item["active"] for item in sources} == {False, True}


def test_rule_document_metadata_cannot_override_revision_authority(database) -> None:
    service = RuleService(database)
    result = service.ingest(
        system_id="dnd5e",
        source_key="rules-current",
        title="Rules",
        content="# Rule\nCurrent procedure.",
        normalized_document=NormalizedDocument(
            content="# Rule\nCurrent procedure.",
            media_type="application/pdf",
            source_path="rules.pdf",
            checksum="a" * 64,
            metadata={
                "logical_source_key": "forged-key",
                "import_state": "retired",
            },
        ),
    )

    source = service.source(result.source_id)
    assert source["source_key"] == "rules-current"
    assert source["active"] is True
    assert source["metadata"]["logical_source_key"] == "rules-current"
    assert "import_state" not in source["metadata"]


def test_rule_chunk_excludes_page_marker_before_next_heading(database) -> None:
    service = RuleService(database)
    service.ingest(
        system_id="dnd5e",
        source_key="appendix",
        title="Appendix",
        content=(
            "<!-- page: 204 -->\n"
            "# Gazer\n"
            "Tiny aberration.\n"
            "## Actions\n"
            "Eye Rays. The gazer shoots two magical rays.\n"
            "<!-- page: 205 -->\n"
            "# Hlam\n"
            "Medium humanoid.\n"
        ),
    )

    eye_rays = service.search(system_id="dnd5e", query="Eye Rays")[0]
    hlam = service.search(system_id="dnd5e", query="Medium humanoid")[0]

    assert eye_rays.metadata["page_start"] == 204
    assert eye_rays.metadata["page_end"] == 204
    assert hlam.metadata["page_start"] == 205


def test_rule_ingest_persists_exact_receipt_and_rolls_back_failed_response(
    database,
) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Atomic rule import",
    )
    rules = RuleService(database)
    idempotency = IdempotencyService(database)
    payload = {"source_key": "atomic-rules"}
    result = rules.ingest(
        system_id="dnd5e",
        source_key="atomic-rules",
        title="Atomic Rules",
        content="# Procedure\nAn exact procedure.",
        idempotency_campaign_id=campaign.id,
        idempotency_key="rule-ingest",
        idempotency_write=IdempotencyWrite(
            scope=f"rule-ingest:{campaign.id}:dm:test",
            payload=payload,
            response=lambda outcome: {
                "source_id": outcome["result"].source_id,
                "metadata": outcome["source_metadata"],
            },
        ),
    )
    receipt = idempotency.lookup(
        f"rule-ingest:{campaign.id}:dm:test",
        "rule-ingest",
        payload,
    )
    assert receipt.response == {
        "source_id": result.source_id,
        "metadata": {
            "logical_source_key": "atomic-rules",
        },
    }

    def fail_response(_outcome):
        raise RuntimeError("rule response failed")

    with pytest.raises(RuntimeError, match="rule response failed"):
        rules.ingest(
            system_id="dnd5e",
            source_key="must-roll-back",
            title="Must Roll Back",
            content="# Failed\nThis source must not commit.",
            idempotency_campaign_id=campaign.id,
            idempotency_key="rule-failure",
            idempotency_write=IdempotencyWrite(
                scope=f"rule-ingest:{campaign.id}:dm:test",
                payload={"source_key": "must-roll-back"},
                response=fail_response,
            ),
        )
    assert (
        rules.search(
            system_id="dnd5e",
            query="This source must not commit",
            source_keys=["must-roll-back"],
        )
        == []
    )
