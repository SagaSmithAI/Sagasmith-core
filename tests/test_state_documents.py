from __future__ import annotations

import pytest

from sagasmith_core import (
    CampaignService,
    CharacterService,
    CharacterStateUpdate,
    EventService,
    MemoryService,
    ModuleService,
    RevisionService,
    RuleProfileService,
    SnapshotService,
    StateMutationService,
)
from sagasmith_core.documents import DocumentBookmark, build_structured_markdown


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
        sheet={"hp": 10},
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
    campaigns.update(campaign.id, state={"door": "open"})
    CharacterService(database).update(character.id, sheet={"hp": 4})
    MemoryService(database).revise(memory.id, content="The cellar door is open.")
    modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=scenes[0]["scene_id"],
        scope_id="player:mira",
        state={"private_discoveries": []},
    )
    restored = saves.restore(campaign.id, first.slot)

    assert restored.parent_id == first.id
    assert campaigns.get(campaign.id).state == {"door": "closed"}
    assert CharacterService(database).get(character.id).sheet == {"hp": 10}
    assert MemoryService(database).list(campaign.id)[0].content.endswith("locked.")
    assert modules.current_scene(campaign.id)["title"] == "Gate"
    mira_scene = modules.current_scene(campaign.id, scope_id="player:mira")
    assert mira_scene["title"] == "Cellar"
    assert mira_scene["progress"]["state"] == {"private_discoveries": ["whisper"]}
    assert saves.verify(campaign.id, restored.slot)
    assert [item.slot for item in saves.lineage(campaign.id)] == [first.slot, restored.slot]
    recap = saves.regenerate_recap(campaign.id, restored.slot)
    assert recap["source"] == "deterministic"


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
