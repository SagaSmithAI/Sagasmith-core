from sagasmith_core.campaigns import CampaignService
from sagasmith_core.items import InventoryService, normalize_inventory
from sagasmith_core.revisions import RevisionService
from sagasmith_core.snapshots import SnapshotService


def test_item_template_instance_move_equip_use_and_history(database) -> None:
    campaigns = CampaignService(database)
    inventory = InventoryService(database)
    revisions = RevisionService(database)

    campaign = campaigns.create(system_id="dnd5e", name="Ledger")
    template = inventory.create_template(
        system_id="dnd5e",
        source_key="srd:potion-healing",
        name="Potion of Healing",
        category="consumable",
        value={"gp": 50},
    )
    item = inventory.add_item(
        campaign_id=campaign.id,
        template_id=template.id,
        name="Potion of Healing",
        owner_type="party",
        owner_id="party",
        quantity=2,
    )
    revisions.record(
        campaign.id,
        operation="item.add",
        entity_type="item_instance",
        entity_id=item.id,
        before=None,
        after=item.__dict__,
    )

    before = inventory.get_item(item.id)
    moved = inventory.move_item(
        item.id,
        owner_type="character",
        owner_id="hero",
    )
    revisions.record(
        campaign.id,
        operation="item.move",
        entity_type="item_instance",
        entity_id=item.id,
        before=before.__dict__,
        after=moved.__dict__,
    )
    equipped = inventory.equip_item(item.id, slot="belt")
    revisions.record(
        campaign.id,
        operation="item.equip",
        entity_type="item_instance",
        entity_id=item.id,
        before=moved.__dict__,
        after=equipped.__dict__,
    )
    before_use = inventory.get_item(item.id)
    used = inventory.use_item(item.id, quantity=1)
    revisions.record(
        campaign.id,
        operation="item.use",
        entity_type="item_instance",
        entity_id=item.id,
        before=before_use.__dict__,
        after=used.__dict__,
    )

    assert moved.owner_type == "character"
    assert equipped.equipped_slot == "belt"
    assert used.quantity == 1
    assert [entry.operation for entry in inventory.history(campaign_id=campaign.id)] == [
        "add",
        "move",
        "equip",
        "use",
    ]

    undone = revisions.undo(campaign.id)
    assert undone.operation == "item.use"
    assert inventory.get_item(item.id).quantity == 2
    redone = revisions.redo(campaign.id)
    assert redone.operation == "item.use"
    assert inventory.get_item(item.id).quantity == 1


def test_snapshot_restores_item_instances(database) -> None:
    campaigns = CampaignService(database)
    inventory = InventoryService(database)
    snapshots = SnapshotService(database)

    campaign = campaigns.create(system_id="dnd5e", name="Snapshot")
    item = inventory.add_item(
        campaign_id=campaign.id,
        name="Iron Key",
        owner_type="party",
        owner_id="party",
    )
    first = snapshots.create(campaign.id, label="before")
    inventory.move_item(item.id, owner_type="location", owner_id="crypt")
    snapshots.restore(campaign.id, first.slot)

    restored = inventory.get_item(item.id)
    assert restored.owner_type == "party"
    assert restored.owner_id == "party"


def test_normalize_legacy_inventory_shapes() -> None:
    values = normalize_inventory(
        [
            "Torch",
            {"name": "Rope", "count": 1, "metadata": {"length": "50 ft"}},
        ]
    )

    assert values[0]["name"] == "Torch"
    assert values[0]["quantity"] == 1
    assert values[1]["state"] == {"length": "50 ft"}
