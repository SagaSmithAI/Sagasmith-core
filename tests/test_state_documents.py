from __future__ import annotations

import pytest
from sqlalchemy import delete, event, select

from sagasmith_core import (
    ActorKnowledgeService,
    BranchService,
    CampaignService,
    CharacterService,
    CharacterStateUpdate,
    ContinuityCommitService,
    ContinuityService,
    EventService,
    IdempotencyService,
    MemoryService,
    ModuleService,
    RevisionService,
    RuleProfileService,
    RuleReceiptService,
    RuleService,
    SnapshotService,
    StateMutationService,
)
from sagasmith_core.documents import (
    DocumentBookmark,
    NormalizedDocument,
    PageLocator,
    PdfDocumentConverter,
    build_structured_markdown,
    normalize_document,
)
from sagasmith_core.models import ActorKnowledgeRevision, SnapshotActorKnowledgeBinding
from sagasmith_core.snapshots import SnapshotIntegrityError


def test_rule_document_path_ingest_preserves_source_and_page_provenance(database, tmp_path) -> None:
    path = tmp_path / "optional-rules.md"
    path.write_text("# Options\n## Tool Synergy\nUse both proficiencies.\n", encoding="utf-8")
    rules = RuleService(database)

    inspection = rules.inspect_path(path)
    assert inspection["sections"] == 2
    assert inspection["checksum"]
    result = rules.ingest_path(
        system_id="dnd5e",
        path=path,
        source_key="optional-rules",
        title="Optional Rules",
        edition="2014",
        publication_id="optional",
    )
    hit = rules.search(system_id="dnd5e", query="Tool Synergy", top_k=1)[0]
    citation = rules.citation(hit.id, source_id=result.source_id)
    expanded = rules.expand(hit.id)
    with pytest.raises(ValueError, match="does not belong"):
        rules.citation(hit.id, source_id="another-source")

    assert hit.metadata["source_checksum"] == inspection["checksum"]
    assert citation["source"] == "rule-source:optional-rules"
    assert citation["source_checksum"] == inspection["checksum"]
    assert expanded["source"]["metadata"]["source_path"] == str(path.resolve())

    path.write_text("# Options\n## Tool Synergy Revised\nNew procedure.\n", encoding="utf-8")
    replaced = rules.ingest_path(
        system_id="dnd5e",
        path=path,
        source_key="optional-rules",
        title="Optional Rules",
        edition="2014",
        publication_id="optional",
    )
    assert replaced.source_id != result.source_id
    revised = rules.search(system_id="dnd5e", query="New procedure", top_k=1)[0]
    assert revised.source_id == replaced.source_id
    assert "New procedure" in revised.content

    paged = NormalizedDocument(
        content=(
            "<!-- page: 7 -->\n# Options\n## Tool Synergy\nUse both proficiencies.\n"
            "<!-- page: 8 -->\nMore guidance.\n"
        ),
        media_type="application/pdf",
        source_path=str(path.resolve()),
        checksum="source-pdf-checksum",
        page_count=8,
    )
    result = rules.ingest(
        system_id="dnd5e",
        source_key="paged-rules",
        title="Paged Rules",
        content=paged.content,
        edition="2014",
        normalized_document=paged,
    )
    hit = rules.search(system_id="dnd5e", query="More guidance", top_k=1)[0]
    citation = rules.citation(hit.id, source_id=result.source_id)
    assert citation["source_checksum"] == "source-pdf-checksum"
    assert citation["page_start"] == 7
    assert citation["page_end"] in {7, 8}


def test_normalized_document_cache_is_content_addressed(tmp_path) -> None:
    source = tmp_path / "rules.md"
    source.write_text("# Rules\n\n## Ready\n\nCached once.\n", encoding="utf-8")
    cache = tmp_path / "cache"

    first = normalize_document(source, cache_dir=cache)
    second = normalize_document(source, cache_dir=cache, expected_checksum=first.checksum)

    assert first.metadata["normalization_cache_hit"] is False
    assert second.metadata["normalization_cache_hit"] is True
    assert second.content == first.content
    assert len(list(cache.rglob("*.json"))) == 1


def test_page_locator_reuses_one_marker_index() -> None:
    content = (
        "<!-- page: 1 -->\nfirst\n"
        "<!-- page: 2 -->\nsecond\n"
        "<!-- page: 20 -->\nlast\n"
    )
    locator = PageLocator(content)

    assert locator.page_for_offset(content.index("first")) == 1
    assert locator.page_for_offset(content.index("second")) == 2
    assert locator.page_for_offset(len(content)) == 20


def test_pdf_converter_ocr_replaces_only_suspect_pages(tmp_path) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    source = tmp_path / "image-only.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=100)
    with source.open("wb") as stream:
        writer.write(stream)

    class FakeOcr:
        name = "fake"

        def __init__(self) -> None:
            self.pages = []

        def extract(self, path, *, page_numbers=None):
            self.pages = list(page_numbers or [])
            return ["RECOVERED HEADING\nRecovered body text for indexing."]

    provider = FakeOcr()
    document = PdfDocumentConverter(ocr_provider=provider).convert(source)

    assert provider.pages == [1]
    assert "RECOVERED HEADING" in document.content
    assert document.metadata["ocr_pages"] == [1]
    assert document.metadata["quality"]["suspect_page_count"] == 0


def test_pdf_page_extraction_cache_survives_normalizer_cache_refresh(tmp_path) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    source = tmp_path / "image-only.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=100)
    with source.open("wb") as stream:
        writer.write(stream)

    class CountingOcr:
        name = "counting"

        def __init__(self) -> None:
            self.calls = 0

        def extract(self, path, *, page_numbers=None):
            self.calls += 1
            return ["RECOVERED HEADING\nRecovered body text for indexing."]

    provider = CountingOcr()
    cache = tmp_path / "cache"
    first = normalize_document(source, ocr_provider=provider, cache_dir=cache)
    for path in cache.glob("*/*.json"):
        path.unlink()
    second = normalize_document(source, ocr_provider=provider, cache_dir=cache)

    assert first.metadata["extraction_cache_hit"] is False
    assert second.metadata["extraction_cache_hit"] is True
    assert provider.calls == 1


def test_pdf_normalization_recovers_unbookmarked_all_caps_subheadings() -> None:
    content, metadata, warnings = build_structured_markdown(
        ["TOOL PROFICIENCIES\nIntro.\nTOOLS AND SKILLS TOGETHER\nOptional procedure."],
        [DocumentBookmark("Tool Proficiencies", 1, 2)],
    )

    assert "#### TOOL PROFICIENCIES" in content
    assert "##### TOOLS AND SKILLS TOGETHER" in content
    assert metadata["heading_count"] == 2
    assert warnings == ()


def test_pdf_normalization_uses_visual_heading_hints_for_mixed_case_titles() -> None:
    content, metadata, warnings = build_structured_markdown(
        [
            "Spell Descriptions\nFireball\n3rd-level evocation\n"
            "Casting Time: 1 action\nRange: 150 feet"
        ],
        [],
        {1: [("Spell Descriptions", 3), ("Fireball", 5)]},
    )

    assert "### Spell Descriptions" in content
    assert "##### Fireball" in content
    assert metadata["matched_visual_headings"] == 2
    assert warnings == ()


def test_pdf_normalization_does_not_treat_uncased_cjk_body_as_all_caps() -> None:
    content, metadata, warnings = build_structured_markdown(
        ["冒险背景 Adventure Background\n这是一行带 D&D 缩写的中文正文\n下一行继续正文。"],
        [],
    )

    assert "##### 这是一行带 D&D 缩写的中文正文" not in content
    assert "这是一行带 D&D 缩写的中文正文下一行继续正文。" in content
    assert metadata["heading_count"] == 0
    assert warnings == ("no structural headings were recovered",)


def test_pdf_normalization_keeps_toc_entries_out_of_heading_hierarchy() -> None:
    content, metadata, _warnings = build_structured_markdown(
        [
            "目录 Contents\n第一章：双城记\n第二章：坠落\n第三章：阿弗纳斯\n"
            "地点一\n地点二\n地点三\n地点四\n地点五\n地点六\n地点七\n地点八",
            "第一章：双城记\nChapter 1\n正文。",
        ],
        [],
    )

    assert metadata["toc_pages"] == [1]
    assert content.count("# 第一章：双城记") == 1
    assert "# 第二章：坠落" not in content


def test_pdf_normalization_does_not_promote_chapter_references_in_body() -> None:
    content, metadata, _warnings = build_structured_markdown(
        [
            "Adventure Overview\n正文从这里开始。\n第一章：双城记\n第二章：坠落\n继续说明。",
            "第二章 埃尔托瑞尔已然坠落\nChapter 2: Elturel Has Fallen\n正文。",
        ],
        [],
    )

    assert "# 第一章：双城记" not in content
    assert "# 第二章：坠落" not in content
    assert content.count("# 第二章 埃尔托瑞尔已然坠落") == 1
    assert "# Chapter 2" not in content
    assert metadata["heading_count"] == 1


def test_campaign_profile_events_snapshot_and_memory(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Branches", state={"door": "closed"})
    RuleProfileService(database).set(
        campaign.id,
        edition="2014",
        locale="zh",
        publications=["srd-5.1"],
    )
    character = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Mira",
        sheet={
            "hp": 10,
            "inventory": [{"id": "healing-potion", "equipped": False}],
            "effects": [{"id": "bless", "remaining_turns": 3}],
        },
        notes={"memories": [{"summary": "Trusts the gate guard."}]},
    )
    EventService(database).add(campaign.id, summary="The door is found")
    memory = MemoryService(database).add(
        campaign.id,
        subject="Door",
        content="The cellar door is locked.",
    )
    modules = ModuleService(database)
    modules.ingest(
        campaign_id=campaign.id,
        source_key="split-party.md",
        title="Split Party",
        content="# Chapter\n## Gate\nOutside.\n## Cellar\nBelow.",
    )
    scenes = modules.scene_index(campaign.id)
    modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=scenes[0]["scene_id"],
        scope_id="party",
    )
    modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=scenes[1]["scene_id"],
        scope_id="player:mira",
        state={"private_discoveries": ["whisper"]},
    )

    saves = SnapshotService(database)
    first = saves.create(campaign.id, label="Before opening")
    assert saves.get(campaign.id, first.slot)["recap"]["summary"] == "Campaign baseline"
    payload = saves.get(campaign.id, first.slot)["payload"]
    assert payload["events"][0]["summary"] == "The door is found"
    assert payload["memories"][0]["revision"]["content"].endswith("locked.")
    assert payload["memories"][0]["fact_key"].startswith("legacy:")
    assert payload["memories"][0]["revision"]["status"] == "active"
    campaigns.update(campaign.id, state={"door": "open"})
    CharacterService(database).update(character.id, sheet={"hp": 4}, notes={"memories": []})
    MemoryService(database).revise(memory.id, content="The cellar door is open.")
    EventService(database).add(campaign.id, summary="The door is opened")
    modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=scenes[0]["scene_id"],
        scope_id="player:mira",
        state={"private_discoveries": []},
    )
    restored = saves.restore(campaign.id, first.slot)

    assert restored.parent_id == first.id
    assert campaigns.get(campaign.id).state == {"door": "closed"}
    restored_character = CharacterService(database).get(character.id)
    assert restored_character.sheet == {
        "hp": 10,
        "inventory": [{"id": "healing-potion", "equipped": False}],
        "effects": [{"id": "bless", "remaining_turns": 3}],
    }
    assert restored_character.notes == {"memories": [{"summary": "Trusts the gate guard."}]}
    assert MemoryService(database).list(campaign.id)[0].content.endswith("locked.")
    assert [item.summary for item in EventService(database).list(campaign.id)] == [
        "The door is found"
    ]
    assert modules.current_scene(campaign.id)["title"] == "Gate"
    mira_scene = modules.current_scene(campaign.id, scope_id="player:mira")
    assert mira_scene["title"] == "Cellar"
    assert mira_scene["progress"]["state"] == {"private_discoveries": ["whisper"]}
    assert saves.verify(campaign.id, restored.slot)
    assert [item.slot for item in saves.lineage(campaign.id)] == [first.slot, restored.slot]
    recap = saves.regenerate_recap(campaign.id, restored.slot)
    assert recap["source"] == "deterministic"


def test_campaign_memory_upsert_has_stable_identity_and_optimistic_revision(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Stable facts")
    memories = MemoryService(database)
    created = memories.upsert(
        campaign.id,
        fact_key="location:cellar:door-state",
        subject="Cellar door",
        subject_ref="location:cellar",
        predicate="door-state",
        content="The cellar door is locked.",
        importance=4,
        disclosure_scope="party",
    )

    updated = memories.upsert(
        campaign.id,
        fact_key="location:cellar:door-state",
        subject="Ignored immutable label",
        content="The cellar door is open.",
        expected_revision_id=created.revision_id,
        source_event_ids=["event:door-opened"],
        importance=5,
        disclosure_scope="public",
    )

    assert updated.id == created.id
    assert updated.revision_id != created.revision_id
    assert updated.fact_key == "location:cellar:door-state"
    assert updated.subject == "Cellar door"
    assert updated.subject_ref == "location:cellar"
    assert updated.predicate == "door-state"
    assert updated.source_event_ids == ["event:door-opened"]
    assert updated.importance == 5
    assert updated.disclosure_scope == "public"
    assert len(memories.list(campaign.id)) == 1

    with pytest.raises(ValueError, match="current revision"):
        memories.upsert(
            campaign.id,
            fact_key="location:cellar:door-state",
            content="A stale writer tries to close it.",
            expected_revision_id=created.revision_id,
        )


def test_campaign_memory_hides_inactive_heads_by_default(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Fact lifecycle")
    memories = MemoryService(database)
    created = memories.upsert(
        campaign.id,
        fact_key="quest:bell:status",
        content="The bell quest is active.",
    )
    retracted = memories.revise(
        created.id,
        content="The bell quest was based on false information.",
        status="retracted",
        expected_revision_id=created.revision_id,
    )

    assert memories.list(campaign.id) == []
    assert memories.search(campaign.id, "bell") == []
    inactive = memories.list(campaign.id, include_inactive=True)
    assert [item.id for item in inactive] == [retracted.id]
    assert [item.revision_id for item in inactive] == [retracted.revision_id]
    assert [item.status for item in inactive] == ["retracted"]


def test_continuity_commit_persists_event_facts_knowledge_and_snapshot_atomically(
    database,
) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Atomic continuity")
    actor = CharacterService(database).create(
        system_id="dnd5e", campaign_id=campaign.id, name="Witness", character_type="pc"
    )

    result = ContinuityCommitService(database).commit(
        campaign.id,
        event={
            "summary": "The witness opens the sealed eastern door.",
            "audience_scope": "actor",
        },
        facts=[
            {
                "fact_key": "location:east-door:state",
                "subject": "Eastern door",
                "subject_ref": "location:east-door",
                "predicate": "state",
                "content": "The eastern door is open.",
                "disclosure_scope": "party",
            }
        ],
        actor_knowledge=[
            {
                "actor_id": actor.id,
                "knowledge_key": "east-door-open",
                "proposition": "I opened the eastern door.",
                "disclosure_scope": "owner",
            }
        ],
        snapshot={"label": "Eastern door opened"},
    )

    assert result["facts"][0]["source_event_ids"] == [result["event"]["id"]]
    assert result["actor_knowledge"][0]["source_event_id"] == result["event"]["id"]
    assert result["snapshot"] is not None
    assert SnapshotService(database).verify(campaign.id, result["snapshot"]["slot"])


def test_continuity_commit_rolls_back_every_ledger_on_failure(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Rollback continuity")
    service = ContinuityCommitService(database)

    with pytest.raises(ValueError, match="live character"):
        service.commit(
            campaign.id,
            event={"summary": "This event must roll back."},
            facts=[
                {
                    "fact_key": "rollback:test",
                    "content": "This fact must roll back.",
                }
            ],
            actor_knowledge=[
                {
                    "actor_id": "missing-actor",
                    "knowledge_key": "impossible",
                    "proposition": "This must fail.",
                }
            ],
        )

    assert EventService(database).list(campaign.id) == []
    assert MemoryService(database).list(campaign.id, include_inactive=True) == []
    event = EventService(database).add(campaign.id, summary="The first committed event")
    assert event.sequence == 1


def test_revision_undo_and_redo(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="coc7e", name="Arkham", state={"clock": 1})
    campaigns.update(campaign.id, state={"clock": 2})
    revisions = RevisionService(database)
    revisions.record(
        campaign.id,
        operation="campaign.state",
        entity_type="campaign",
        entity_id=campaign.id,
        before={"state": {"clock": 1}},
        after={"state": {"clock": 2}},
    )

    revisions.undo(campaign.id)
    assert campaigns.get(campaign.id).state == {"clock": 1}
    revisions.redo(campaign.id)
    assert campaigns.get(campaign.id).state == {"clock": 2}


def test_campaign_character_is_an_independent_library_instance(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Instances")
    characters = CharacterService(database)
    template = characters.create(
        system_id="dnd5e",
        name="Mira Template",
        character_type="pc",
        sheet={"hp": 10, "inventory": [{"id": "key"}]},
        notes={"profile": {"summary": "A careful explorer."}},
    )
    instance = characters.instantiate(
        template.id,
        campaign_id=campaign.id,
        name="Mira",
        player_name="Ada",
        sheet={"hp": 9, "inventory": [{"id": "key"}], "edition": "2024"},
    )

    assert instance.id != template.id
    assert instance.template_id == template.id
    assert instance.campaign_id == campaign.id
    assert instance.sheet["edition"] == "2024"
    assert [item.id for item in characters.list_library(system_id="dnd5e")] == [template.id]

    characters.update(instance.id, sheet={"hp": 4, "inventory": []})
    assert characters.get(template.id).sheet == {
        "hp": 10,
        "inventory": [{"id": "key"}],
    }

    snapshot = SnapshotService(database).create(campaign.id, label="Template instance")
    characters.update(instance.id, sheet={"hp": 1, "inventory": []})
    characters.update(template.id, notes={"profile": {"summary": "Updated library copy."}})
    SnapshotService(database).restore(campaign.id, snapshot.slot)

    assert characters.get(instance.id).sheet["hp"] == 4
    assert characters.get(template.id).notes["profile"]["summary"] == "Updated library copy."


def test_character_build_creates_template_and_instance_atomically(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Build")
    template, instance = CharacterService(database).create_with_instance(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Mira",
        character_type="pc",
        player_name="Ada",
        sheet={"hp": 10},
        notes={"profile": {"summary": "A newly built hero."}},
    )

    assert template.campaign_id is None
    assert template.player_name is None
    assert instance.campaign_id == campaign.id
    assert instance.player_name == "Ada"
    assert instance.template_id == template.id
    assert instance.sheet == template.sheet


def test_character_build_replays_template_and_instance_atomically(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Build replay")
    characters = CharacterService(database)
    arguments = {
        "system_id": "dnd5e",
        "campaign_id": campaign.id,
        "name": "Mira",
        "character_type": "pc",
        "player_name": "Ada",
        "sheet": {"hp": 10},
        "notes": {"profile": {"summary": "A newly built hero."}},
        "principal_id": "dm:ada",
        "idempotency_key": "build-mira",
    }

    first = characters.create_with_instance(**arguments)
    replay = characters.create_with_instance(**arguments)

    assert replay[0].id == first[0].id
    assert replay[1].id == first[1].id
    assert [item.id for item in characters.list_library(system_id="dnd5e")] == [first[0].id]
    assert [item.id for item in characters.list(system_id="dnd5e", campaign_id=campaign.id)] == [
        first[1].id
    ]


def test_snapshot_restore_preserves_its_undo_cursor_and_retires_future_revisions(
    database,
) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Undo branch", state={"clock": 0})
    revisions = RevisionService(database)
    snapshots = SnapshotService(database)

    campaigns.update(campaign.id, state={"clock": 1})
    revisions.record(
        campaign.id,
        operation="campaign.state",
        entity_type="campaign",
        entity_id=campaign.id,
        before={"state": {"clock": 0}},
        after={"state": {"clock": 1}},
    )
    saved = snapshots.create(campaign.id, label="Clock one")

    campaigns.update(campaign.id, state={"clock": 2})
    revisions.record(
        campaign.id,
        operation="campaign.state",
        entity_type="campaign",
        entity_id=campaign.id,
        before={"state": {"clock": 1}},
        after={"state": {"clock": 2}},
    )
    snapshots.restore(campaign.id, saved.slot)

    assert campaigns.get(campaign.id).state == {"clock": 1}
    with pytest.raises(LookupError, match="nothing to redo"):
        revisions.redo(campaign.id)
    revisions.undo(campaign.id)
    assert campaigns.get(campaign.id).state == {"clock": 0}


def test_snapshot_restore_rolls_back_every_step_when_materialization_fails(
    database, monkeypatch
) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Atomic restore")
    snapshots = SnapshotService(database)
    target = snapshots.create(campaign.id, label="target")
    branches = BranchService(database)
    original_branch = branches.current(campaign.id)
    original_slots = [item.slot for item in snapshots.list(campaign.id)]

    def fail_apply(*_args, **_kwargs) -> None:
        raise RuntimeError("materialization failed")

    monkeypatch.setattr(snapshots, "_apply", fail_apply)

    with pytest.raises(RuntimeError, match="materialization failed"):
        snapshots.restore(campaign.id, target.slot)

    assert [item.slot for item in snapshots.list(campaign.id)] == original_slots
    assert [item.id for item in branches.list(campaign.id)] == [original_branch.id]
    assert branches.current(campaign.id).id == original_branch.id


def test_branch_scoped_facts_events_and_actor_knowledge_do_not_leak(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Knowledge branches")
    actor = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Guard",
        character_type="npc",
        sheet={},
        notes={},
    )
    events = EventService(database)
    memories = MemoryService(database)
    knowledge = ActorKnowledgeService(database)
    snapshots = SnapshotService(database)

    witnessed = events.add(campaign.id, summary="The guard sees the cellar key")
    fact = memories.add(campaign.id, subject="Cellar key", content="The key is in the cellar.")
    belief = knowledge.add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="cellar-key-location",
        proposition="The key is in the cellar.",
        source_event_id=witnessed.id,
    )
    base = snapshots.create(campaign.id, label="Key seen")
    main = BranchService(database).current(campaign.id)

    memories.revise(fact.id, content="The key is now in the guard room.")
    knowledge.revise(
        belief.id,
        proposition="The key was moved to the guard room.",
        epistemic_status="belief",
    )
    events.add(campaign.id, summary="The key is moved")
    snapshots.create(campaign.id, label="Key moved")

    alternate = BranchService(database).create(
        campaign.id,
        name="key-stays-put",
        from_snapshot_id=base.id,
        checkout=True,
    )
    snapshots.checkout_branch(campaign.id, alternate.id)

    assert memories.list(campaign.id)[0].content == "The key is in the cellar."
    assert knowledge.list(campaign.id, actor_id=actor.id)[0].proposition.endswith("cellar.")
    assert [item.summary for item in events.list(campaign.id)] == ["The guard sees the cellar key"]

    assert memories.list(campaign.id, branch_id=main.id)[0].content.endswith("guard room.")
    assert knowledge.list(campaign.id, actor_id=actor.id, branch_id=main.id)[
        0
    ].proposition.endswith("guard room.")


def test_event_and_all_witness_knowledge_commit_or_rollback_together(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Atomic witnesses")
    characters = CharacterService(database)
    first = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="First", character_type="pc"
    )
    second = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Second", character_type="pc"
    )
    third = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Third", character_type="pc"
    )
    events = EventService(database)
    knowledge = ActorKnowledgeService(database)

    event, knowledge_ids = events.add_with_actor_knowledge(
        campaign.id,
        summary="First and second see the sigil.",
        actor_ids=[first.id, second.id],
        knowledge_key="sigil",
        proposition="The sigil is blue.",
        audience_scope="party",
    )
    assert len(knowledge_ids) == 2
    assert knowledge.list(campaign.id, actor_id=first.id)[0].source_event_id == event.id
    assert knowledge.list(campaign.id, actor_id=second.id)[0].source_event_id == event.id

    with pytest.raises(ValueError, match="knowledge key already exists"):
        events.add_with_actor_knowledge(
            campaign.id,
            summary="This write must fully roll back.",
            actor_ids=[third.id, first.id],
            knowledge_key="sigil",
            proposition="A conflicting observation.",
        )

    assert [item.summary for item in events.list(campaign.id)] == [
        "First and second see the sigil."
    ]
    assert knowledge.list(campaign.id, actor_id=third.id) == []


def test_actor_scoped_events_follow_visible_actor_knowledge(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Separate witnesses")
    characters = CharacterService(database)
    witness = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Witness", character_type="pc"
    )
    unaware = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Unaware", character_type="pc"
    )
    event = EventService(database).add(
        campaign.id,
        event_type="revelation",
        summary="The witness sees the masked visitor leave.",
        audience_scope="actor",
    )
    ActorKnowledgeService(database).add(
        campaign.id,
        actor_id=witness.id,
        knowledge_key="masked-visitor-departed",
        proposition="The masked visitor left by the east door.",
        source_event_id=event.id,
        disclosure_scope="owner",
    )
    continuity = ContinuityService(database)

    seen = continuity.context(
        campaign.id,
        actor_id=witness.id,
        audience="player",
        query="masked visitor",
    )
    hidden = continuity.context(
        campaign.id,
        actor_id=unaware.id,
        audience="player",
        query="masked visitor",
    )

    assert [item["id"] for item in seen["events"]] == [event.id]
    assert [item["knowledge_key"] for item in seen["actor_knowledge"]] == [
        "masked-visitor-departed"
    ]
    assert hidden["events"] == []
    assert hidden["actor_knowledge"] == []


def test_actor_event_authorization_is_not_limited_by_knowledge_top_n(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Recall window")
    actor = CharacterService(database).create(
        system_id="dnd5e", campaign_id=campaign.id, name="Witness", character_type="pc"
    )
    knowledge = ActorKnowledgeService(database)
    knowledge.add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="decoy-query-match",
        proposition="The decoy query is memorable.",
        disclosure_scope="owner",
    )
    event = EventService(database).add(
        campaign.id,
        summary="A masked courier leaves through the east door.",
        audience_scope="actor",
    )
    knowledge.add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="courier-departure",
        proposition="The masked courier left through the east door.",
        source_event_id=event.id,
        disclosure_scope="owner",
    )

    context = ContinuityService(database).context(
        campaign.id,
        actor_id=actor.id,
        audience="player",
        query="decoy query",
        limit=1,
    )

    assert [item["knowledge_key"] for item in context["actor_knowledge"]] == [
        "decoy-query-match"
    ]
    assert [item["id"] for item in context["events"]] == [event.id]


def test_continuity_context_applies_one_shared_budget_with_metrics(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Budgeted context")
    memories = MemoryService(database)
    for index in range(5):
        memories.add(
            campaign.id,
            fact_key=f"fact:budget:{index}",
            content=f"Matched clue {index} " + ("x" * 360),
            importance=5 - min(index, 4),
            disclosure_scope="party",
        )

    context = ContinuityService(database).context(
        campaign.id,
        query="matched clue",
        audience="player",
        budget_chars=1_000,
    )

    assert context["retrieval"]["strategy"] == "lexical_structured_shared_budget_v2"
    assert context["retrieval"]["budget_chars"] == 1_000
    assert context["retrieval"]["candidate_count"] == 5
    assert context["retrieval"]["returned_count"] < 5
    assert context["retrieval"]["truncated"] is True


def test_continuity_diagnostics_reports_ledger_and_snapshot_health(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Diagnostics")
    event = EventService(database).add(
        campaign.id,
        summary="The gate opens.",
        audience_scope="party",
    )
    memory = MemoryService(database).add(
        campaign.id,
        fact_key="location:gate:state",
        content="The gate is open.",
        source_event_ids=[event.id, "event:missing"],
        disclosure_scope="party",
    )
    SnapshotService(database).create(campaign.id, label="Gate opened")
    MemoryService(database).revise(
        memory.id,
        content="The gate is no longer relevant.",
        expected_revision_id=memory.revision_id,
        status="superseded",
    )
    EventService(database).add(
        campaign.id,
        summary="A later event is not snapshotted.",
        audience_scope="dm",
    )

    diagnostics = ContinuityService(database).diagnostics(campaign.id)

    assert diagnostics["facts"] == {
        "total": 1,
        "active": 0,
        "inactive": 1,
        "orphan_source_event_refs": 1,
    }
    assert diagnostics["events"]["unsnapshotted"] == 1
    assert diagnostics["events"]["latest_sequence"] == 2
    assert diagnostics["snapshots"]["total_on_branch"] == 1
    assert diagnostics["snapshots"]["latest_payload_chars"] > 0


def test_event_and_actor_knowledge_reject_unknown_visibility_scopes(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Visibility enums")
    actor = CharacterService(database).create(
        system_id="dnd5e", campaign_id=campaign.id, name="Witness", character_type="pc"
    )
    with pytest.raises(ValueError, match="event audience scope"):
        EventService(database).add(
            campaign.id,
            summary="Invalid audience",
            audience_scope="somebody",
        )
    with pytest.raises(ValueError, match="actor-knowledge disclosure scope"):
        ActorKnowledgeService(database).add(
            campaign.id,
            actor_id=actor.id,
            knowledge_key="invalid-scope",
            proposition="This must be rejected.",
            disclosure_scope="somebody",
        )


def test_snapshot_recap_only_contains_party_safe_deltas(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Safe recap")
    characters = CharacterService(database)
    hero = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Hero", character_type="pc"
    )
    hidden_npc = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Hidden Spy", character_type="npc"
    )
    snapshots = SnapshotService(database)
    snapshots.create(campaign.id, label="Before changes")

    characters.update(hidden_npc.id, summary="The hidden spy changed plans.")
    characters.update(hero.id, summary="The hero was wounded.")
    EventService(database).add(
        campaign.id,
        summary="The party reached the bridge.",
        audience_scope="party",
    )
    EventService(database).add(
        campaign.id,
        summary="The spy poisoned the well.",
        audience_scope="dm",
    )
    MemoryService(database).add(
        campaign.id,
        content="The spy poisoned the well.",
        metadata={"disclosure_scope": "dm"},
    )
    saved = snapshots.create(campaign.id, label="After changes")
    recap = snapshots.get(campaign.id, saved.slot)["recap"]

    assert recap["characters"] == ["Hero"]
    assert recap["events"] == ["The party reached the bridge."]
    assert recap["memory_candidates"] == []
    assert "Hidden Spy" not in str(recap)
    assert "poisoned" not in str(recap)


def test_snapshot_presentation_recap_is_subordinate_and_evidence_bound(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Recap evidence")
    snapshots = SnapshotService(database)
    snapshots.create(campaign.id, label="Baseline")
    visible = EventService(database).add(
        campaign.id,
        summary="The party reaches the bridge.",
        audience_scope="party",
    )
    hidden = EventService(database).add(
        campaign.id,
        summary="The hidden spy leaves.",
        audience_scope="dm",
    )

    saved = snapshots.create(
        campaign.id,
        label="Bridge",
        recap={
            "summary": "We reached the bridge.",
            "evidence_event_ids": [visible.id],
        },
    )
    recap = snapshots.get(campaign.id, saved.slot)["recap"]

    assert recap["source"] == "deterministic"
    assert recap["events"] == ["The party reaches the bridge."]
    assert recap["presentation"]["summary"] == "We reached the bridge."
    assert recap["provenance"]["evidence_event_ids"] == [visible.id]
    with pytest.raises(ValueError, match="outside the player-safe delta"):
        snapshots.create(
            campaign.id,
            label="Unsafe presentation",
            recap={"summary": "Leak", "evidence_event_ids": [hidden.id]},
        )


def test_same_actor_knowledge_key_can_diverge_independently_on_sibling_branches(
    database,
) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Sibling beliefs")
    actor = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Witness",
        character_type="npc",
    )
    snapshots = SnapshotService(database)
    branches = BranchService(database)
    knowledge = ActorKnowledgeService(database)
    base = snapshots.create(campaign.id, label="Before the clue")
    main = branches.current(campaign.id)
    alternate = branches.create(
        campaign.id,
        name="alternate-clue",
        from_snapshot_id=base.id,
    )

    main_value = knowledge.add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="masked-visitor",
        subject_ref="npc:visitor",
        proposition="The visitor wore a red mask.",
    )
    snapshots.create(campaign.id, label="Main sees red")
    snapshots.checkout_branch(campaign.id, alternate.id)
    alternate_value = knowledge.add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="masked-visitor",
        subject_ref="npc:visitor",
        proposition="The visitor wore a blue mask.",
    )

    assert alternate_value.id == main_value.id
    assert alternate_value.revision_id != main_value.revision_id
    assert knowledge.list(campaign.id, actor_id=actor.id, branch_id=main.id)[
        0
    ].proposition.endswith("red mask.")
    assert knowledge.list(campaign.id, actor_id=actor.id, branch_id=alternate.id)[
        0
    ].proposition.endswith("blue mask.")


def test_actor_knowledge_cannot_cite_an_event_from_another_branch(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Event causality")
    actor = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Witness",
        character_type="npc",
    )
    snapshots = SnapshotService(database)
    base = snapshots.create(campaign.id, label="Before split")
    event = EventService(database).add(campaign.id, summary="Only the main branch sees this")
    fact = MemoryService(database).add(
        campaign.id,
        subject="Main-only fact",
        content="Only the main branch recorded this.",
    )
    snapshots.create(campaign.id, label="Main-only event")
    alternate = BranchService(database).create(
        campaign.id,
        name="did-not-see-event",
        from_snapshot_id=base.id,
    )
    snapshots.checkout_branch(campaign.id, alternate.id)

    with pytest.raises(LookupError, match="not visible on branch"):
        MemoryService(database).revise(
            fact.id,
            content="This must not import the fact into the sibling branch.",
        )
    with pytest.raises(LookupError, match="not visible on branch"):
        ActorKnowledgeService(database).add(
            campaign.id,
            actor_id=actor.id,
            knowledge_key="impossible-witness",
            proposition="I saw the main-branch event.",
            source_event_id=event.id,
        )


def test_snapshot_is_full_and_validates_actor_knowledge_bindings(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Full save")
    actor = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Archivist",
        character_type="npc",
        sheet={"hp": 7},
    )
    ActorKnowledgeService(database).add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="sealed-door",
        proposition="The eastern door is sealed.",
    )
    snapshots = SnapshotService(database)
    saved = snapshots.create(campaign.id, label="Complete state")
    document = snapshots.get(campaign.id, saved.slot)

    assert document["storage_mode"] == "full"
    assert document["payload"]["campaign"]["name"] == "Full save"
    assert document["payload"]["characters"][0]["sheet"] == {"hp": 7}
    assert document["payload"]["actor_knowledge"][0]["knowledge_key"] == "sealed-door"
    assert document["valid"] is True

    with database.transaction() as session:
        revision_id = session.scalar(
            select(SnapshotActorKnowledgeBinding.revision_id).where(
                SnapshotActorKnowledgeBinding.snapshot_id == saved.id
            )
        )
        session.get(ActorKnowledgeRevision, revision_id).proposition = "Tampered ledger value."
    assert snapshots.verify(campaign.id, saved.slot) is False
    with pytest.raises(SnapshotIntegrityError, match="wrong revision"):
        snapshots.restore(campaign.id, saved.slot)
    with database.transaction() as session:
        session.get(ActorKnowledgeRevision, revision_id).proposition = "The eastern door is sealed."
    assert snapshots.verify(campaign.id, saved.slot) is True

    with database.transaction() as session:
        session.execute(
            delete(SnapshotActorKnowledgeBinding).where(
                SnapshotActorKnowledgeBinding.snapshot_id == saved.id
            )
        )

    assert snapshots.verify(campaign.id, saved.slot) is False
    with pytest.raises(SnapshotIntegrityError, match="actor-knowledge bindings"):
        snapshots.restore(campaign.id, saved.slot)


def test_restore_head_recaptures_materialized_actors_and_actor_knowledge(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Restore capture")
    characters = CharacterService(database)
    actor = characters.create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Witness",
        character_type="pc",
        sheet={"hp": 7},
    )
    ActorKnowledgeService(database).add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="sealed-door",
        proposition="The eastern door is sealed.",
        disclosure_scope="owner",
    )
    snapshots = SnapshotService(database)
    saved = snapshots.create(campaign.id, label="Witness knows")
    characters.update(actor.id, sheet={"hp": 1})

    restored = snapshots.restore(campaign.id, saved.slot)
    document = snapshots.get(campaign.id, restored.slot)

    assert document["valid"] is True
    assert document["payload"]["characters"] == [
        {
            "id": actor.id,
            "system_id": "dnd5e",
            "character_type": "pc",
            "template_id": None,
            "name": "Witness",
            "player_name": None,
            "summary": "",
            "sheet": {"hp": 7},
            "notes": {},
            "revision": 1,
        }
    ]
    assert [item["knowledge_key"] for item in document["payload"]["actor_knowledge"]] == [
        "sealed-door"
    ]


def test_snapshot_head_flag_tracks_all_branch_refs_and_parent_cannot_be_forged(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="DAG heads")
    snapshots = SnapshotService(database)
    base = snapshots.create(campaign.id, label="Base")
    BranchService(database).create(
        campaign.id,
        name="still-at-base",
        from_snapshot_id=base.id,
    )
    next_save = snapshots.create(campaign.id, label="Main advances")

    heads = {item.id: item.is_head for item in snapshots.list(campaign.id)}
    assert heads == {base.id: True, next_save.id: True}
    with pytest.raises(ValueError, match="checked-out branch head"):
        snapshots.create(campaign.id, label="Forged ancestry", parent_id=base.id)


def test_branch_checkout_refuses_to_mix_unsaved_state_with_saved_continuity(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Clean checkout", state={"clock": 0})
    snapshots = SnapshotService(database)
    branches = BranchService(database)

    with pytest.raises(ValueError, match="snapshot before branching"):
        branches.create(campaign.id, name="no-baseline")

    base = snapshots.create(campaign.id, label="Clock zero")
    main = branches.current(campaign.id)
    alternate = branches.create(
        campaign.id,
        name="clock-zero-copy",
        from_snapshot_id=base.id,
    )
    campaigns.update(campaign.id, state={"clock": 1})

    with pytest.raises(ValueError, match="unsaved changes"):
        snapshots.checkout_branch(campaign.id, alternate.id)
    assert branches.current(campaign.id).id == main.id
    assert campaigns.get(campaign.id).state == {"clock": 1}

    snapshots.create(campaign.id, label="Clock one")
    snapshots.checkout_branch(campaign.id, alternate.id)
    assert campaigns.get(campaign.id).state == {"clock": 0}


def test_branch_checkout_bulk_restores_large_revision_cursor(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Bulk cursor", state={"step": -1})
    mutations = StateMutationService(database)
    snapshots = SnapshotService(database)
    branches = BranchService(database)

    for step in range(60):
        current = campaigns.get(campaign.id)
        mutations.replace(
            campaign.id,
            campaign_state={"step": step},
            expected_campaign_revision=current.revision,
            operation="test.bulk-cursor",
            idempotency_key=f"bulk-cursor-{step}",
        )
    base = snapshots.create(campaign.id, label="Sixty revisions")
    alternate = branches.create(
        campaign.id,
        name="bulk-cursor-copy",
        from_snapshot_id=base.id,
    )
    current = campaigns.get(campaign.id)
    mutations.replace(
        campaign.id,
        campaign_state={"step": 61},
        expected_campaign_revision=current.revision,
        operation="test.bulk-cursor",
        idempotency_key="bulk-cursor-main-only",
    )
    snapshots.create(campaign.id, label="Main advances")

    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record_statement)
    try:
        snapshots.checkout_branch(campaign.id, alternate.id)
    finally:
        event.remove(database.engine, "before_cursor_execute", record_statement)

    assert campaigns.get(campaign.id).state == {"step": 59}
    revision_statements = [
        statement
        for statement in statements
        if "state_revisions" in statement.casefold()
    ]
    assert len(revision_statements) <= 12


def test_state_mutation_replaces_campaign_and_character_documents_atomically(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Mutations")
    characters = CharacterService(database)
    hero = characters.create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Mira",
        sheet={"wallet": {"gp": 1}},
        notes={"memories": []},
    )

    StateMutationService(database).replace(
        campaign.id,
        campaign_state={"party": {"wallet": {"gp": 2}}},
        character_updates=[
            CharacterStateUpdate(
                character_id=hero.id,
                expected_revision=hero.revision,
                sheet={"wallet": {"gp": 0}},
                notes={"memories": [{"summary": "Paid the party fund."}]},
            )
        ],
    )

    assert CampaignService(database).get(campaign.id).state["party"]["wallet"] == {"gp": 2}

    updated = characters.get(hero.id)
    assert updated.sheet["wallet"] == {"gp": 0}
    assert updated.notes["memories"][0]["summary"] == "Paid the party fund."

    with pytest.raises(ValueError, match="campaign revision conflict"):
        StateMutationService(database).replace(
            campaign.id,
            campaign_state={"party": {"wallet": {"gp": 3}}},
            expected_campaign_revision=0,
        )
    assert CampaignService(database).get(campaign.id).state["party"]["wallet"] == {"gp": 2}

    with pytest.raises(ValueError):
        StateMutationService(database).replace(
            campaign.id,
            campaign_state={"party": {"wallet": {"gp": 99}}},
            character_updates=[
                CharacterStateUpdate(
                    character_id=hero.id,
                    expected_revision=hero.revision,
                    sheet={},
                    notes={},
                )
            ],
        )

    assert CampaignService(database).get(campaign.id).state["party"]["wallet"] == {"gp": 2}


def test_state_mutation_exposes_committed_idempotency_recovery_without_a_receipt(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Receipt recovery")
    StateMutationService(database).replace(
        campaign.id,
        campaign_state={"phase": "after"},
        operation="test.receipt.recovery",
        idempotency_key="recover-on-retry",
    )
    assert IdempotencyService(database).mutation_committed(campaign.id, "recover-on-retry")
    with pytest.raises(ValueError, match="committed mutation group"):
        StateMutationService(database).replace(
            campaign.id,
            campaign_state={"phase": "duplicated"},
            operation="test.receipt.recovery",
            idempotency_key="recover-on-retry",
        )
    assert CampaignService(database).get(campaign.id).state == {"phase": "after"}


def test_state_mutation_persists_rule_receipts_in_the_same_group(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Rule receipts")
    revisions = StateMutationService(database).replace(
        campaign.id,
        campaign_state={"phase": "resolved"},
        operation="test.rule.receipt",
        rule_receipts=[
            {
                "mechanic_id": "dnd5e.core.activity.accounting",
                "event": "activity.after",
                "operations": [],
                "citations": [{"source": "SRD", "section": "Actions"}],
                "ruleset_fingerprint": "a" * 64,
            }
        ],
    )

    receipts = RuleReceiptService(database).list(campaign.id)
    assert len(receipts) == 1
    assert receipts[0].mutation_group_id == revisions[0].mutation_group_id
    assert receipts[0].branch_id is not None
    assert receipts[0].mechanic_id == "dnd5e.core.activity.accounting"
    assert receipts[0].receipt["citations"][0]["section"] == "Actions"
    assert receipts[0].operation == "test.rule.receipt"
    assert receipts[0].applied is True

    snapshot = SnapshotService(database).create(campaign.id, label="After settlement")
    fork = BranchService(database).create(
        campaign.id,
        name="receipt-fork",
        from_snapshot_id=snapshot.id,
    )
    fork_receipts = RuleReceiptService(database).list(campaign.id, branch_id=fork.id)
    assert len(fork_receipts) == 1
    assert fork_receipts[0].branch_id == fork.id
    assert fork_receipts[0].mutation_group_id != receipts[0].mutation_group_id
    assert fork_receipts[0].receipt == receipts[0].receipt

    RevisionService(database).undo(campaign.id)
    assert (
        RuleReceiptService(database).list(campaign.id, branch_id=receipts[0].branch_id)[0].applied
        is False
    )
    assert RuleReceiptService(database).list(campaign.id, branch_id=fork.id)[0].applied is True


def test_pdf_normalization_and_module_generator_structure(database) -> None:
    content, stats, warnings = build_structured_markdown(
        [
            "Book Header\n目录\n第一章：目录项\n1",
            "Book Header\n第一章 正文\nChapter 1\n运作本章\n正文。\nA1. Gate\n房间。\n2",
        ],
        [DocumentBookmark("运作本章", 2, 0)],
    )
    assert "Book Header" not in content
    assert "<!-- page: 2 -->" in content
    assert stats["matched_bookmarks"] == 1
    assert not warnings

    campaign = CampaignService(database).create(system_id="dnd5e", name="Generated")
    result = ModuleService(database).ingest(
        campaign_id=campaign.id,
        source_key="generated.md",
        title="Generated",
        content=(
            "# 第一章\n"
            "## 酒馆\n线索出现。\n"
            "### 遭遇\n敌人靠近。\n"
            "#### A1. 地窖\n门后有宝箱。\n"
            "## 广场\n群众聚集。\n"
            "# 附录\n"
            "## NPC\n| 姓名 | 目标 |\n|---|---|\n| 米拉 | 逃离 |\n"
        ),
    )
    assert result.chapters == 2
    assert result.scenes >= 3
    hit = ModuleService(database).search(campaign_id=campaign.id, query="宝箱")[0]
    assert hit.title == "酒馆"
