from __future__ import annotations

import pytest
from sqlalchemy import select

from sagasmith_core.access import AccessService
from sagasmith_core.actor_lifecycle import ActorLifecycleService, InitialActorGrant
from sagasmith_core.branches import BranchService
from sagasmith_core.campaigns import CampaignService
from sagasmith_core.characters import CharacterService
from sagasmith_core.idempotency import IdempotencyConflictError
from sagasmith_core.models import ActorGrant, Character
from sagasmith_core.revisions import RevisionService
from sagasmith_core.snapshots import SnapshotService


def test_actor_lifecycle_create_retry_undo_and_redo_are_one_mutation(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="test", name="Lifecycle", state={"combat": {}})
    access = AccessService(database)
    access.ensure_principal("user:dm")
    access.grant_campaign(campaign.id, "user:dm", role="dm")
    template = CharacterService(database).create(
        system_id="test",
        name="Template",
        character_type="npc",
        sheet={"hp": 4},
        notes={"source": "library"},
    )
    service = ActorLifecycleService(database)
    state = {"combat": {"active": True, "participants": ["pending"]}}

    created = service.create(
        campaign.id,
        system_id="test",
        name="Instance",
        character_type="npc",
        sheet={"hp": 8},
        notes={"source": "validated"},
        template_id=template.id,
        principal_id="user:dm",
        idempotency_key="create-1",
        initial_grants=(InitialActorGrant("user:dm"),),
        campaign_state=state,
        expected_campaign_revision=campaign.revision,
    )
    replay = service.create(
        campaign.id,
        system_id="test",
        name="Instance",
        character_type="npc",
        sheet={"hp": 8},
        notes={"source": "validated"},
        template_id=template.id,
        principal_id="user:dm",
        idempotency_key="create-1",
        initial_grants=(InitialActorGrant("user:dm"),),
        campaign_state=state,
        expected_campaign_revision=campaign.revision,
    )
    assert replay.replayed is True
    assert replay.character.id == created.character.id
    assert replay.mutation_group_id == created.mutation_group_id
    assert created.character.template_id == template.id
    assert campaigns.get(campaign.id).state == state

    with database.transaction() as session:
        assert session.get(Character, created.character.id) is not None
        assert session.scalar(
            select(ActorGrant).where(ActorGrant.actor_id == created.character.id)
        ) is not None

    RevisionService(database).undo(campaign.id)
    assert campaigns.get(campaign.id).state == {"combat": {}}
    with database.transaction() as session:
        assert session.get(Character, created.character.id) is None
        assert session.scalar(
            select(ActorGrant).where(ActorGrant.actor_id == created.character.id)
        ) is None

    RevisionService(database).redo(campaign.id)
    restored = CharacterService(database).get(created.character.id)
    assert restored.template_id == template.id
    assert restored.sheet == {"hp": 8}
    assert campaigns.get(campaign.id).state == state
    assert access.require_actor(
        campaign.id,
        created.character.id,
        "user:dm",
        control=True,
        private=True,
    )


def test_actor_lifecycle_retry_rejects_changed_payload(database) -> None:
    campaign = CampaignService(database).create(system_id="test", name="Conflict")
    access = AccessService(database)
    access.ensure_principal("user:dm")
    access.grant_campaign(campaign.id, "user:dm", role="dm")
    service = ActorLifecycleService(database)
    arguments = {
        "campaign_id": campaign.id,
        "system_id": "test",
        "name": "Actor",
        "character_type": "npc",
        "sheet": {},
        "notes": {},
        "principal_id": "user:dm",
        "idempotency_key": "same-key",
    }
    service.create(**arguments)
    with pytest.raises(IdempotencyConflictError):
        service.create(**{**arguments, "name": "Different"})


def test_actor_lifecycle_grants_follow_snapshot_branch_checkout(database) -> None:
    campaign = CampaignService(database).create(system_id="test", name="Branches")
    access = AccessService(database)
    access.ensure_principal("user:dm")
    access.grant_campaign(campaign.id, "user:dm", role="dm")
    lifecycle = ActorLifecycleService(database)
    first = lifecycle.create(
        campaign.id,
        system_id="test",
        name="First",
        character_type="npc",
        sheet={},
        notes={},
        principal_id="user:dm",
        idempotency_key="first",
        initial_grants=(InitialActorGrant("user:dm"),),
    )
    snapshots = SnapshotService(database)
    branches = BranchService(database)
    first_head = snapshots.create(campaign.id, label="First actor")
    original = branches.current(campaign.id)
    fork = branches.create(
        campaign.id,
        name="Second actor branch",
        from_snapshot_id=first_head.id,
        checkout=True,
    )
    second = lifecycle.create(
        campaign.id,
        system_id="test",
        name="Second",
        character_type="npc",
        sheet={},
        notes={},
        principal_id="user:dm",
        idempotency_key="second",
        initial_grants=(InitialActorGrant("user:dm"),),
        branch_id=fork.id,
    )
    snapshots.create(campaign.id, label="Second actor")

    branches.checkout(campaign.id, original.id)
    assert CharacterService(database).get(first.character.id).name == "First"
    with pytest.raises(Exception, match=second.character.id):
        CharacterService(database).get(second.character.id)
    assert access.require_actor(
        campaign.id,
        first.character.id,
        "user:dm",
        control=True,
        private=True,
    )
    with database.transaction() as session:
        assert session.scalar(
            select(ActorGrant).where(ActorGrant.actor_id == second.character.id)
        ) is None

    branches.checkout(campaign.id, fork.id)
    assert access.require_actor(
        campaign.id,
        second.character.id,
        "user:dm",
        control=True,
        private=True,
    )
