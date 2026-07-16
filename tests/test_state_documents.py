from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from sagasmith_core import (
    ActorKnowledgeService,
    BranchService,
    CampaignService,
    CharacterService,
    CharacterStateUpdate,
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
    build_structured_markdown,
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


def test_pdf_normalization_recovers_unbookmarked_all_caps_subheadings() -> None:
    content, metadata, warnings = build_structured_markdown(
        ["TOOL PROFICIENCIES\nIntro.\nTOOLS AND SKILLS TOGETHER\nOptional procedure."],
        [DocumentBookmark("Tool Proficiencies", 1, 2)],
    )

    assert "#### TOOL PROFICIENCIES" in content
    assert "##### TOOLS AND SKILLS TOGETHER" in content
    assert metadata["heading_count"] == 2
    assert warnings == ()


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
