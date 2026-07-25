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

    by_id = service.search(
        system_id="dnd5e", query="Shared", source_ids=[first.source_id]
    )
    by_key = service.search(
        system_id="dnd5e", query="Shared", source_keys=["book-b"]
    )

    assert {hit.source_id for hit in by_id} == {first.source_id}
    assert {hit.metadata["source_key"] for hit in by_key} == {"book-b"}


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

