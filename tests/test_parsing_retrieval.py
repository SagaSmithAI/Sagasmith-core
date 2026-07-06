import math

from sagasmith_core.modules import MarkdownModuleParser
from sagasmith_core.parsing import MarkdownHierarchyParser
from sagasmith_core.retrieval import (
    enrich_query,
    lexical_score,
    reciprocal_rank_fusion,
    structured_score,
)


def test_markdown_parser_preserves_heading_paths() -> None:
    parsed = MarkdownHierarchyParser(chunk_size=200, chunk_overlap=20).parse(
        "# Combat\nGeneral rules.\n## Grapple\nA grapple uses a check."
    )

    assert [section.path for section in parsed] == [
        ("Combat",),
        ("Combat", "Grapple"),
    ]
    assert parsed[1].chunks[0].heading_path == ("Combat", "Grapple")


def test_module_parser_supports_profiles_without_scene_boundary_hook() -> None:
    class LegacyProfile:
        name = "legacy"
        version = "1"

        @staticmethod
        def classify_chunk(_heading: str, _text: str) -> str:
            return "narrative"

        @staticmethod
        def keywords(_title: str, _text: str) -> list[str]:
            return []

    chapters = MarkdownModuleParser(profile=LegacyProfile()).parse(
        "# Chapter\n## Gate\nDescription.\n"
    )

    assert chapters[0].scenes[0].title == "Gate"


def test_lexical_search_handles_chinese_and_english() -> None:
    assert lexical_score("擒抱 grapple", title="Grapple 擒抱", content="Rules") > 0


def test_rrf_combines_rankings_deterministically() -> None:
    fused = reciprocal_rank_fusion(
        {"lexical": ["a", "b"], "dense": ["b", "a"]},
    )

    assert {item[0] for item in fused[:2]} == {"a", "b"}


def test_enrich_query_appends_english_aliases() -> None:
    expanded = enrich_query("豁免")
    assert "save" in expanded or "saving" in expanded


def test_enrich_query_preserves_unchanged_query_when_no_match() -> None:
    assert enrich_query("xyz123noop") == "xyz123noop"


def test_enrich_query_merges_extra_terms() -> None:
    expanded = enrich_query("狂暴", extra_terms={"狂暴": ["rage", "frenzy"]})
    assert "rage" in expanded
    assert "frenzy" in expanded


def test_structured_score_prefers_multi_field_match() -> None:
    unstructured = structured_score(
        "宝箱",
        content="There is a room with a chest behind the curtain.",
    )
    structured = structured_score(
        "宝箱",
        keywords="宝箱 treasure chest locked",
        content="The door creaks open.",
    )
    assert structured > unstructured


def test_structured_score_uses_field_weights() -> None:
    title_hit = structured_score(
        "Gate",
        scene_title="Gate",
        content="Nothing here.",
    )
    content_hit = structured_score(
        "Gate",
        scene_title="Other",
        content="The Gate is open. Many gates.",
    )
    # Title match (weight 4.0) should beat partial content match (weight 1.0)
    assert title_hit > content_hit


def test_structured_score_skips_empty_fields() -> None:
    score = structured_score("combat", tags="combat", content="")
    assert score > 0
    assert not math.isnan(score)


def test_fts5_query_handles_cjk_and_english() -> None:
    from sagasmith_core.retrieval import fts5_query

    cjk = fts5_query("豁免检定")
    assert cjk is not None
    assert "+" in cjk

    english = fts5_query("fireball")
    assert english == "fireball"

    mixed = fts5_query("豁免 save combat")
    assert mixed is not None
    assert "+" in mixed
    assert "save" in mixed
    assert "combat" in mixed

    empty = fts5_query("")
    assert empty is None

    special = fts5_query("fireball (damage)")
    assert special is not None and "(" not in special


def test_fts5_hits_produces_results_on_sqlite(database) -> None:
    from sagasmith_core.campaigns import CampaignService
    from sagasmith_core.database import alembic_config
    from sagasmith_core.modules import ModuleService
    from sagasmith_core.retrieval import fts5_hits
    from alembic import command

    db = database
    campaign = CampaignService(db).create(system_id="dnd5e", name="FTS")

    service = ModuleService(db)
    service.ingest(
        campaign_id=campaign.id,
        source_key="fts_demo.md",
        title="FTS Demo",
        content="# Ch1\n## Gate\nGuarded by wolves.\n## Library\nBooks about fireball.",
    )

    # Run the migration to create FTS5 tables
    config = alembic_config(db.url)
    command.upgrade(config, "head")

    with db.transaction() as session:
        hits = fts5_hits(session, "module_fts", "wolves", limit=5)
    assert len(hits) >= 1, f"expected at least 1 hit for 'wolves', got {hits}"

    with db.transaction() as session:
        hits_cjk = fts5_hits(session, "module_fts", "fireball", limit=5)
    assert len(hits_cjk) >= 1, f"expected at least 1 hit for 'fireball', got {hits_cjk}"

    # Search through the public API should also use FTS5
    api_hits = service.search(campaign_id=campaign.id, query="wolves")
    assert len(api_hits) >= 1
    assert "Gate" in api_hits[0].title

