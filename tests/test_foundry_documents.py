from sagasmith_core import CampaignService, FoundryDocumentService, SnapshotService


def test_foundry_style_documents_form_actor_item_activity_effect_message_chain(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Runtime")
    documents = FoundryDocumentService(database)

    actor = documents.create_actor(
        campaign_id=campaign.id,
        system_id="dnd5e",
        actor_type="character",
        name="Mira",
        system={"abilities": {"str": {"value": 16}}},
    )
    item = documents.create_item(
        campaign_id=campaign.id,
        system_id="dnd5e",
        actor_id=actor.id,
        item_type="weapon",
        name="Longsword",
        system={"equipped": True},
    )
    activity = documents.create_activity(
        item_id=item.id,
        activity_type="attack",
        name="Slash",
        activation={"type": "action", "cost": 1},
        consumption={"targets": []},
        target={"type": "creature", "value": 1},
    )
    effect = documents.create_effect(
        campaign_id=campaign.id,
        parent_type="actor",
        parent_id=actor.id,
        actor_id=actor.id,
        name="Blessed",
        changes=[{"key": "system.bonuses.abilities.save", "mode": "ADD", "value": "1d4"}],
        statuses=["blessed"],
    )
    message = documents.create_message(
        campaign_id=campaign.id,
        message_type="activity",
        actor_id=actor.id,
        item_id=item.id,
        activity_id=activity.id,
        rolls=[{"formula": "1d20+5", "total": 18}],
        narration_hints=["Mira's longsword connects."],
    )

    assert documents.list_actors(campaign.id)[0].name == "Mira"
    assert documents.list_items(campaign.id, actor_id=actor.id)[0].name == "Longsword"
    assert documents.list_activities(item.id)[0].activation["type"] == "action"
    assert documents.list_effects(campaign.id, actor_id=actor.id)[0].id == effect.id
    assert documents.list_messages(campaign.id)[0].sequence == message.sequence

    updated = documents.update_activity(activity.id, uses={"spent": 1, "max": 1})
    assert documents.get_activity(activity.id).uses == updated.uses
    assert documents.get_actor(actor.id).system["abilities"]["str"]["value"] == 16


def test_snapshot_restores_foundry_style_documents(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Runtime")
    documents = FoundryDocumentService(database)
    actor = documents.create_actor(
        campaign_id=campaign.id,
        system_id="dnd5e",
        actor_type="character",
        name="Mira",
    )
    item = documents.create_item(
        campaign_id=campaign.id,
        system_id="dnd5e",
        actor_id=actor.id,
        item_type="feat",
        name="Action Surge",
    )
    documents.create_activity(
        item_id=item.id,
        activity_type="utility",
        name="Action Surge",
        uses={"spent": 0, "max": 1, "recovery": [{"period": "shortRest"}]},
    )
    snapshot = SnapshotService(database).create(campaign.id, label="Before effect")

    documents.create_effect(
        campaign_id=campaign.id,
        parent_type="actor",
        parent_id=actor.id,
        actor_id=actor.id,
        name="Invisible",
        statuses=["invisible"],
    )
    assert documents.list_effects(campaign.id)

    SnapshotService(database).restore(campaign.id, snapshot.slot)
    assert documents.list_actors(campaign.id)[0].name == "Mira"
    assert documents.list_items(campaign.id, actor_id=actor.id)[0].name == "Action Surge"
    assert documents.list_effects(campaign.id) == []
