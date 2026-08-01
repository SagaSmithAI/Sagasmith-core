from __future__ import annotations

import pytest

from sagasmith_core import (
    CampaignService,
    ContinuityCommitService,
    MemoryService,
    SubjectContextService,
    validate_subject_context_fact,
)


def test_faction_context_excludes_world_truth_and_other_factions(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Factions")
    memories = MemoryService(database)
    own_state = memories.add(
        campaign.id,
        fact_key="faction:ember:goal",
        kind="faction_state",
        subject_ref="faction:ember",
        predicate="goal",
        content="Preserve the alliance.",
    )
    own_knowledge = memories.add(
        campaign.id,
        fact_key="faction:ember:knows-gate",
        kind="faction_knowledge",
        subject_ref="faction:ember",
        predicate="knows",
        content="The north gate is weak.",
    )
    memories.add(
        campaign.id,
        fact_key="world:ember:hidden-vault",
        kind="world",
        subject_ref="faction:ember",
        predicate="secret",
        content="A hidden vault exists below the court.",
    )
    memories.add(
        campaign.id,
        fact_key="faction:ash:goal",
        kind="faction_state",
        subject_ref="faction:ash",
        predicate="goal",
        content="Undermine the alliance.",
    )

    context = SubjectContextService(database).list(
        campaign.id,
        subject_ref="faction:ember",
    )

    assert {item.id for item in context} == {own_state.id, own_knowledge.id}
    assert {item.kind for item in context} == {"faction_state", "faction_knowledge"}


@pytest.mark.parametrize(
    ("kind", "subject_ref", "message"),
    [
        ("actor_state", "faction:ember", "actor:<id>"),
        ("faction_state", "actor:npc", "faction:<id>"),
        ("faction_knowledge", "scene:forge", "faction:<id>"),
    ],
)
def test_subject_context_fact_kind_must_match_owner(
    kind: str,
    subject_ref: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_subject_context_fact(kind=kind, subject_ref=subject_ref)


def test_memory_service_enforces_subject_context_ownership(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Ownership")

    with pytest.raises(ValueError, match="faction:<id>"):
        MemoryService(database).add(
            campaign.id,
            fact_key="faction:ember:bad-owner",
            kind="faction_knowledge",
            subject_ref="actor:npc",
            predicate="knows",
            content="This must not be stored under an actor.",
        )


def test_actor_subject_context_contains_state_but_not_world_facts(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Actors")
    memories = MemoryService(database)
    state = memories.add(
        campaign.id,
        fact_key="actor:npc:goal",
        kind="actor_state",
        subject_ref="actor:npc",
        predicate="goal",
        content="Escape safely.",
    )
    memories.add(
        campaign.id,
        fact_key="world:npc:parentage",
        kind="world",
        subject_ref="actor:npc",
        predicate="identity",
        content="The NPC is the duke's child.",
    )

    context = SubjectContextService(database).list(
        campaign.id,
        subject_ref="actor:npc",
    )

    assert [item.id for item in context] == [state.id]


def test_fact_key_cannot_change_subject_identity_through_upsert(database) -> None:
    campaign = CampaignService(database).create(system_id="neutral", name="Identity")
    memories = MemoryService(database)
    original = memories.add(
        campaign.id,
        fact_key="shared:key",
        kind="fact",
        subject="World record",
        subject_ref="world:one",
        predicate="state",
        content="Original world state.",
    )

    with pytest.raises(ValueError, match="fact_key identity conflict.*kind"):
        memories.upsert(
            campaign.id,
            fact_key="shared:key",
            kind="actor_state",
            subject_ref="actor:npc-2",
            predicate="goal",
            content="NPC overwrite.",
            expected_revision_id=original.revision_id,
        )

    stored = memories.list(campaign.id)[0]
    assert stored.content == "Original world state."
    assert stored.subject_ref == "world:one"


def test_atomic_continuity_commit_rejects_fact_key_identity_collision(database) -> None:
    campaign = CampaignService(database).create(system_id="neutral", name="Atomic identity")
    original = MemoryService(database).add(
        campaign.id,
        fact_key="shared:key",
        kind="fact",
        subject_ref="world:one",
        predicate="state",
        content="Original world state.",
    )

    with pytest.raises(ValueError, match="fact_key identity conflict.*kind"):
        ContinuityCommitService(database).commit(
            campaign.id,
            event={"summary": "The invalid overwrite must roll back."},
            facts=[
                {
                    "action": "upsert",
                    "fact_key": "shared:key",
                    "kind": "actor_state",
                    "subject_ref": "actor:npc-2",
                    "predicate": "goal",
                    "content": "NPC overwrite.",
                    "expected_revision_id": original.revision_id,
                }
            ],
        )

    assert MemoryService(database).list(campaign.id)[0].content == "Original world state."
