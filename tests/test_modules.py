from sagasmith_core.campaigns import CampaignService
from sagasmith_core.modules import ModuleService


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
    assert service.current_scene(
        campaign.id,
        scope_id="player:alice",
    )["title"] == "Broken Gate"
    inherited = service.current_scene(campaign.id, scope_id="player:bob")
    assert inherited["title"] == "Inner Hall"
    assert inherited["inherited_from_party"] is True
    assert service.current_scene(campaign.id)["title"] == "Inner Hall"
