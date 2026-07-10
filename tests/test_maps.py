from sagasmith_core.campaigns import CampaignService
from sagasmith_core.maps import MapService
from sagasmith_core.revisions import RevisionService
from sagasmith_core.snapshots import SnapshotService


def test_map_scene_token_region_snapshot_and_undo(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Map")
    maps = MapService(database)
    revisions = RevisionService(database)
    snapshots = SnapshotService(database)

    scene = maps.create_scene(campaign.id, name="Cellar", width=1200, height=800)
    token = maps.create_token(
        scene.id,
        actor_type="character",
        actor_id="hero",
        name="Hero",
        x=10,
        y=20,
    )
    region = maps.create_region(
        scene.id,
        name="Web",
        shape={"type": "circle", "x": 5, "y": 5, "radius": 15},
        behavior="difficult_terrain",
        attached_token_id=token.id,
        duration={"period": "declared_minute", "value": 10},
    )
    assert region.attached_token_id == token.id

    snap = snapshots.create(campaign.id, label="before move")
    before = maps.get_token(token.id)
    moved = maps.move_token(token.id, x=30, y=40)
    revisions.record(
        campaign.id,
        operation="token.move",
        entity_type="scene_token",
        entity_id=token.id,
        before=before.__dict__,
        after=moved.__dict__,
    )
    assert maps.get_token(token.id).x == 30
    updated_token = maps.update_token(
        token.id,
        name="Hidden Hero",
        hidden=True,
        disposition="friendly",
        vision={"darkvision": 60},
        metadata={"controlled": True},
    )
    assert updated_token.name == "Hidden Hero"
    assert updated_token.hidden is True
    assert updated_token.disposition == "friendly"
    assert updated_token.vision["darkvision"] == 60
    assert updated_token.metadata["controlled"] is True

    revisions.undo(campaign.id)
    assert maps.get_token(token.id).x == 10

    maps.move_token(token.id, x=50, y=60)
    snapshots.restore(campaign.id, snap.slot)
    assert maps.get_token(token.id).x == 10


def test_region_can_be_read_updated_and_deleted(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Region lifecycle")
    maps = MapService(database)
    scene = maps.create_scene(campaign.id, name="Cellar")
    region = maps.create_region(
        scene.id,
        name="Fog Cloud",
        shape={"type": "circle", "x": 10, "y": 10, "radius": 20},
        duration={"remaining": 2},
    )

    assert maps.get_region(region.id).duration["remaining"] == 2
    updated = maps.update_region(
        region.id,
        duration={"remaining": 1},
        metadata={"origin": "spell:fog-cloud"},
    )
    assert updated.duration["remaining"] == 1
    assert updated.metadata["origin"] == "spell:fog-cloud"
    assert maps.delete_region(region.id).id == region.id
    assert maps.list_regions(scene.id) == []
