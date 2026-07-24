from __future__ import annotations

import pytest
from sqlalchemy import event

from sagasmith_core import (
    AccessDeniedError,
    AccessService,
    CampaignService,
    CharacterService,
    IdempotencyConflictError,
    IdempotencyService,
    RevisionService,
    StateMutationService,
)
from sagasmith_core.idempotency import request_hash


def test_grouped_revision_undo_redo_is_atomic(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Grouped")
    hero = CharacterService(database).create(
        system_id="dnd5e", campaign_id=campaign.id, name="Hero", sheet={"gp": 1}, notes={}
    )
    characters = CharacterService(database)
    updated = characters.update(hero.id, sheet={"gp": 2}, notes={})
    revisions = RevisionService(database)
    revisions.record_group(
        campaign.id,
        operation="test.transfer",
        changes=[
            {
                "entity_type": "campaign",
                "entity_id": campaign.id,
                "before": {"state": campaign.state, "revision": campaign.revision},
                "after": {"state": {"gp": 5}, "revision": campaign.revision + 1},
            },
            {
                "entity_type": "character",
                "entity_id": hero.id,
                "before": {"sheet": hero.sheet, "notes": hero.notes, "revision": hero.revision},
                "after": {
                    "sheet": updated.sheet,
                    "notes": updated.notes,
                    "revision": updated.revision,
                },
            },
        ],
    )
    CampaignService(database).update(campaign.id, state={"gp": 5})

    revisions.undo(campaign.id)
    assert CampaignService(database).get(campaign.id).state == campaign.state
    assert CharacterService(database).get(hero.id).sheet == hero.sheet
    revisions.redo(campaign.id)
    assert CampaignService(database).get(campaign.id).state == {"gp": 5}


def test_principal_membership_and_actor_grants_are_explicit(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Access")
    actor = CharacterService(database).create(
        system_id="dnd5e", campaign_id=campaign.id, name="Mira", sheet={}, notes={}
    )
    access = AccessService(database)
    access.ensure_principal("user:alice", platform="test", external_id="alice")
    access.grant_campaign(campaign.id, "user:alice", role="player")
    with pytest.raises(AccessDeniedError):
        access.require_actor(campaign.id, actor.id, "user:alice", control=True)
    access.grant_actor(campaign.id, "user:alice", actor.id, can_control=True, can_view_private=True)
    assert access.require_actor(campaign.id, actor.id, "user:alice", control=True)


def test_campaign_role_cannot_forge_unknown_actor(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Access owner")
    access = AccessService(database)
    access.ensure_principal("user:dm", platform="test", external_id="dm")
    access.grant_campaign(campaign.id, "user:dm", role="dm")
    with pytest.raises(AccessDeniedError):
        access.require_actor(campaign.id, "not-an-actor", "user:dm", control=True)


def test_idempotency_rejects_key_reuse_with_different_payload(database) -> None:
    service = IdempotencyService(database)
    service.remember("campaign:c1", "request-1", {"amount": 1}, {"ok": True})
    replay = service.lookup("campaign:c1", "request-1", {"amount": 1})
    assert replay is not None and replay.replayed is True
    with pytest.raises(IdempotencyConflictError):
        service.lookup("campaign:c1", "request-1", {"amount": 2})


def test_campaign_idempotency_receipt_recovers_response_without_stale_request(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Receipt")
    service = IdempotencyService(database)
    revisions = StateMutationService(database).replace(
        campaign.id,
        campaign_state={"rested": True},
        expected_campaign_revision=campaign.revision,
        operation="campaign.party.rest.long_rest",
        idempotency_key="long-rest-1",
    )
    assert revisions is not None
    service.remember(
        f"campaign:{campaign.id}",
        "long-rest-1",
        {"expected_revision": 4},
        {"status": "committed", "world_time": {"elapsed_minutes": 480}},
        campaign_id=campaign.id,
    )

    receipt = service.receipt(campaign.id, "long-rest-1")

    assert receipt.replayed is True
    assert receipt.response == {
        "status": "committed",
        "world_time": {"elapsed_minutes": 480},
    }
    assert receipt.request_hash == request_hash({"expected_revision": 4})
    assert receipt.mutation_group_id == revisions[0].mutation_group_id
    assert receipt.entity_revisions == [
        {
            "entity_type": "campaign",
            "entity_id": campaign.id,
            "before_revision": campaign.revision,
            "after_revision": campaign.revision + 1,
        }
    ]
    with pytest.raises(LookupError, match="not found"):
        service.receipt(campaign.id, "missing")


def test_revision_head_queries_are_bounded_in_sql(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Bounded history")
    mutations = StateMutationService(database)
    mutations.replace(
        campaign.id,
        campaign_state={"step": 1},
        expected_campaign_revision=campaign.revision,
        operation="test.step",
        idempotency_key="step-1",
    )
    campaign = CampaignService(database).get(campaign.id)
    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(" ".join(statement.upper().split()))

    event.listen(database.engine, "before_cursor_execute", capture_statement)
    try:
        mutations.replace(
            campaign.id,
            campaign_state={"step": 2},
            expected_campaign_revision=campaign.revision,
            operation="test.step",
            idempotency_key="step-2",
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", capture_statement)

    head_queries = [
        statement
        for statement in statements
        if "FROM STATE_REVISIONS JOIN MUTATION_GROUPS" in statement
        and "ORDER BY STATE_REVISIONS.SEQUENCE DESC" in statement
    ]
    assert head_queries
    assert all(" LIMIT " in statement for statement in head_queries)
