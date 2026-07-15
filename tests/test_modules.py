from sagasmith_core.campaigns import CampaignService
from sagasmith_core.modules import ModuleService
from sagasmith_core.snapshots import SnapshotService


def test_module_ingest_search_and_progress(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Road")
    service = ModuleService(database)
    result = service.ingest(
        campaign_id=campaign.id,
        source_key="keep.md",
        title="The Keep",
        content=(
            "# Chapter One\nArrival.\n"
            "## Broken Gate\nThe gate is guarded by two wolves.\n"
            "## Inner Hall\nA sealed door leads below."
        ),
    )

    hits = service.search(campaign_id=campaign.id, query="wolves")
    progress = service.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=hits[0].metadata["scene_id"],
        progress=40,
        current_room="Gate",
        state={"wolves_defeated": False},
    )
    preserved = service.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=hits[0].metadata["scene_id"],
        progress=50,
    )

    assert result.chapters == 1
    assert result.scenes == 2
    assert hits[0].title == "Broken Gate"
    assert hits[0].metadata["scene_type"] == "section"
    assert hits[0].metadata["visibility"] == "keeper"
    assert progress["progress"] == 40
    assert preserved["current_room"] == "Gate"
    assert preserved["state"] == {"wolves_defeated": False}
    current = service.current_scene(campaign.id)
    assert current is not None
    assert current["title"] == "Broken Gate"
    assert current["progress"]["percent"] == 50
    assert current["progress"]["state"] == {"wolves_defeated": False}
    index = service.scene_index(campaign.id)
    assert [item["title"] for item in index] == ["Broken Gate", "Inner Hall"]
    assert index[0]["visibility"] == "keeper"
    assert index[0]["clues"] == []
    assert index[0]["stable_key"] == "chapter-one-broken-gate"
    assert index[0]["chapter_ordinal"] == 0
    assert index[0]["scene_ordinal"] == 0

    service.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=index[1]["scene_id"],
        progress=5,
    )
    assert service.current_scene(campaign.id)["title"] == "Inner Hall"

    scoped = service.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=index[0]["scene_id"],
        scope_id="player:alice",
        progress=70,
        state={"discovered": ["wolf tracks"]},
    )
    assert scoped["scope_id"] == "player:alice"
    assert (
        service.current_scene(
            campaign.id,
            scope_id="player:alice",
        )["title"]
        == "Broken Gate"
    )
    inherited = service.current_scene(campaign.id, scope_id="player:bob")
    assert inherited["title"] == "Inner Hall"
    assert inherited["inherited_from_party"] is True
    assert service.current_scene(campaign.id)["title"] == "Inner Hall"
    projected = service.scene_progress_index(campaign.id, scope_id="player:alice")
    assert [item["percent"] for item in projected] == [70, 5]
    assert projected[0]["inherited_from_party"] is False
    assert projected[1]["inherited_from_party"] is True


def test_module_reimport_preserves_snapshot_scene_references(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Revision")
    modules = ModuleService(database)
    modules.ingest(
        campaign_id=campaign.id,
        source_key="keep.md",
        title="The Keep",
        content="# Chapter\n## Gate\nThe original gate.",
    )
    original = modules.scene_index(campaign.id)[0]
    modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=original["scene_id"],
        current_location_key="gate",
        state={"door": "closed"},
    )
    snapshot = SnapshotService(database).create(campaign.id, label="Before revision")

    modules.ingest(
        campaign_id=campaign.id,
        source_key="keep.md",
        title="The Keep",
        content="# Chapter\n## Courtyard\nThe revised entry.",
    )

    assert [item["title"] for item in modules.scene_index(campaign.id)] == ["Courtyard"]
    assert modules.current_scene(campaign.id)["title"] == "Gate"
    assert modules.current_scene(campaign.id)["progress"]["current_location_key"] == "gate"
    restored = SnapshotService(database).restore(campaign.id, snapshot.slot)
    assert restored.parent_id == snapshot.id
    assert modules.current_scene(campaign.id)["title"] == "Gate"
