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

    revisions.undo(campaign.id)
    assert maps.get_token(token.id).x == 10

    maps.move_token(token.id, x=50, y=60)
    snapshots.restore(campaign.id, snap.slot)
    assert maps.get_token(token.id).x == 10
