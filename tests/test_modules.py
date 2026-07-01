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
        state={"wolves_defeated": False},
    )

    assert result.chapters == 1
    assert result.scenes == 2
    assert hits[0].title == "Broken Gate"
    assert progress["progress"] == 40
    index = service.scene_index(campaign.id)
    assert [item["title"] for item in index] == ["Broken Gate", "Inner Hall"]
