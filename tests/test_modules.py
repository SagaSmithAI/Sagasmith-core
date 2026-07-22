from sagasmith_core.campaigns import CampaignService
from sagasmith_core.documents import NormalizedDocument
from sagasmith_core.modules import MarkdownModuleParser, ModuleService, SceneBoundary
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


def test_module_parser_preserves_front_matter_before_first_chapter() -> None:
    chapters = MarkdownModuleParser().parse(
        "<!-- page: 1 -->\n## Adventure Overview\nThe city has fallen.\n"
        "<!-- page: 2 -->\n# Chapter One\n## Arrival\nThe party arrives.\n"
    )

    assert [chapter.title for chapter in chapters] == ["Front Matter", "Chapter One"]
    assert chapters[0].scenes[0].title == "Adventure Overview"
    assert "city has fallen" in chapters[0].content
    assert chapters[0].metadata["page_start"] == 1
    assert chapters[1].metadata["page_start"] == 2
    assert chapters[1].scenes[0].metadata["page_start"] == 2


def test_module_preview_exposes_scene_page_and_line_provenance(database, tmp_path) -> None:
    source = tmp_path / "module.md"
    source.write_text(
        "<!-- page: 7 -->\n# Chapter One\n\n## Arrival\n\nText.\n",
        encoding="utf-8",
    )

    preview = ModuleService(database).preview_path(source)

    assert preview["valid"] is True
    assert preview["scenes"][0]["page_start"] == 7
    assert preview["scenes"][0]["page_end"] == 7
    assert preview["scenes"][0]["start_line"] is not None
    assert preview["scenes"][0]["end_line"] is not None


def test_scene_stable_keys_preserve_cjk_chapter_identity(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="中文章节")
    service = ModuleService(database)
    result = service.ingest(
        campaign_id=campaign.id,
        source_key="chapters.md",
        title="章节",
        content="# 第一章\n## 发展\n甲。\n# 第二章\n## 发展\n乙。\n",
    )

    assert result.scenes == 2
    assert [item["stable_key"] for item in service.scene_index(campaign.id)] == [
        "第一章-发展",
        "第二章-发展",
    ]


def test_scene_stable_keys_disambiguate_repeated_headings(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="重复场景")
    service = ModuleService(database)
    service.ingest(
        campaign_id=campaign.id,
        source_key="repeated.md",
        title="Repeated",
        content="# Chapter\n## Development\nFirst.\n## Development\nSecond.\n",
    )

    assert [item["stable_key"] for item in service.scene_index(campaign.id)] == [
        "chapter-development",
        "chapter-development--2",
    ]


def test_staged_module_reparses_same_content_when_parser_version_changes(database) -> None:
    class VersionedProfile:
        name = "test"

        def __init__(self, version: str) -> None:
            self.version = version

        def classify_chunk(self, heading: str, text: str) -> str:
            return "narrative"

        def keywords(self, title: str, text: str) -> list[str]:
            return []

        def scene_boundaries(self, chapter_title: str, chapter_content: str):
            return [SceneBoundary(f"Version {self.version}", 0, len(chapter_content))]

    campaign = CampaignService(database).create(system_id="dnd5e", name="Parser revisions")
    service = ModuleService(database)
    content = "# Chapter\nBody.\n"
    first = service.ingest(
        campaign_id=campaign.id,
        source_key="module",
        logical_source_key="module",
        title="Module",
        content=content,
        parser=MarkdownModuleParser(profile=VersionedProfile("1")),
        activate=False,
    )
    second = service.ingest(
        campaign_id=campaign.id,
        source_key="module",
        logical_source_key="module",
        title="Module",
        content=content,
        parser=MarkdownModuleParser(profile=VersionedProfile("2")),
        activate=False,
    )

    assert first.module_id != second.module_id
    assert first.skipped is False
    assert second.skipped is False


def test_scene_progress_can_reference_one_spatial_location_in_the_same_module(database) -> None:
    class SpatialProfile:
        name = "spatial-test"
        version = "1"

        def classify_chunk(self, heading: str, text: str) -> str:
            return "narrative"

        def keywords(self, title: str, text: str) -> list[str]:
            return []

        def scene_boundaries(self, chapter_title: str, chapter_content: str):
            split = chapter_content.index("## Ambush")
            return [
                SceneBoundary(
                    "Tavern Locations",
                    0,
                    split,
                    metadata={
                        "spatial": {
                            "schema_version": 1,
                            "locations": [{"key": "e7-upstairs", "title": "E7"}],
                        }
                    },
                ),
                SceneBoundary(
                    "Ambush",
                    split,
                    len(chapter_content),
                    metadata={
                        "spatial": {
                            "schema_version": 1,
                            "locations": [{"key": "ambush", "title": "Ambush"}],
                        }
                    },
                ),
            ]

    campaign = CampaignService(database).create(system_id="dnd5e", name="Cross-scene map")
    modules = ModuleService(database)
    modules.ingest(
        campaign_id=campaign.id,
        source_key="tavern.md",
        title="Tavern",
        content="# Chapter\n## Locations\nE7 upstairs.\n## Ambush\nPirates arrive.\n",
        parser=MarkdownModuleParser(profile=SpatialProfile()),
    )
    scenes = modules.scene_index(campaign.id)
    ambush = next(item for item in scenes if item["title"] == "Ambush")

    progress = modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=ambush["scene_id"],
        current_location_key="e7-upstairs",
    )

    assert progress["current_location_key"] == "e7-upstairs"


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


def test_reviewed_visual_connections_merge_and_restore_with_scene_progress(
    database, tmp_path
) -> None:
    class SpatialProfile:
        name = "spatial-review"
        version = "1"

        def classify_chunk(self, heading: str, text: str) -> str:
            return "room"

        def keywords(self, title: str, text: str) -> list[str]:
            return []

        def scene_boundaries(self, chapter_title: str, chapter_content: str):
            return [
                SceneBoundary(
                    "Dungeon",
                    0,
                    len(chapter_content),
                    metadata={
                        "spatial": {
                            "schema_version": 1,
                            "grid": {"kind": "square", "cell_ft": 5},
                            "locations": [
                                {"key": "d5", "title": "D5"},
                                {"key": "d6", "title": "D6"},
                                {"key": "d7", "title": "D7"},
                            ],
                            "connections": [],
                        }
                    },
                )
            ]

    campaign = CampaignService(database).create(system_id="dnd5e", name="Reviewed map")
    source = tmp_path / "dungeon.pdf"
    source.write_bytes(b"test-pdf")
    content = "# Chapter\n## Dungeon\nD5. Entry\nD6. Morgue\nD7. Altar\n"
    modules = ModuleService(database)
    imported = modules.ingest(
        campaign_id=campaign.id,
        source_key="dungeon.pdf",
        title="Dungeon",
        content=content,
        parser=MarkdownModuleParser(profile=SpatialProfile()),
        normalized_document=NormalizedDocument(
            content=content,
            media_type="application/pdf",
            source_path=str(source),
            checksum="a" * 64,
            page_count=30,
        ),
    )
    scene = modules.scene_index(campaign.id)[0]
    asset = modules.list_assets(campaign.id, imported.module_id)[0]
    reviewed = modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=scene["scene_id"],
        expected_state_version=0,
        progress=40,
        current_location_key="d5",
        spatial_review={
            "source_asset_id": asset["id"],
            "page_number": 22,
            "reviewer": "dm:test",
            "branch_id": "branch-main",
            "connections": [
                {
                    "from": "d5",
                    "to": "d6",
                    "kind": "passage",
                    "observation": "The map draws an open corridor between D5 and D6.",
                }
            ],
        },
    )
    snapshot = SnapshotService(database).create(campaign.id, label="Reviewed D5-D6")
    replaced = modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=scene["scene_id"],
        expected_state_version=reviewed["state_version"],
        spatial_review={
            "mode": "replace",
            "source_asset_id": asset["id"],
            "page_number": 22,
            "reviewer": "dm:test",
            "branch_id": "branch-main",
            "connections": [
                {
                    "from": "d6",
                    "to": "d7",
                    "kind": "door",
                    "observation": "Replacement review for restore verification.",
                }
            ],
        },
    )

    assert replaced["state"]["spatial_review"]["connections"][0]["from"] == "d6"
    assert replaced["progress"] == 40
    current = modules.current_scene(campaign.id)
    assert current["spatial"]["connections"][0]["confidence"] == "reviewed_image"
    SnapshotService(database).restore(campaign.id, snapshot.slot)
    restored = modules.current_scene(campaign.id)
    assert restored["progress"]["state"]["spatial_review"]["connections"][0]["to"] == "d6"
    assert restored["spatial"]["review"]["connection_count"] == 1


def test_image_only_module_content_can_be_reviewed_with_page_evidence(database, tmp_path) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Reviewed content")
    source = tmp_path / "creatures.pdf"
    source.write_bytes(b"test-pdf")
    content = "# Appendix D\n## Cultists\nThe statblock is printed as an image.\n"
    modules = ModuleService(database)
    imported = modules.ingest(
        campaign_id=campaign.id,
        source_key="creatures.pdf",
        title="Creatures",
        content=content,
        normalized_document=NormalizedDocument(
            content=content,
            media_type="application/pdf",
            source_path=str(source),
            checksum="b" * 64,
            page_count=20,
        ),
    )
    scene = modules.scene_index(campaign.id)[0]
    asset = modules.list_assets(campaign.id, imported.module_id)[0]
    markdown = "# Necromite\n\n*Medium humanoid, neutral evil*"
    reviewed = modules.review_content(
        campaign_id=campaign.id,
        module_id=imported.module_id,
        scene_id=scene["scene_id"],
        content_key="necromite-of-myrkul",
        content_kind="dnd5e_2014_statblock",
        normalized_content=markdown,
        source_asset_id=asset["id"],
        page_number=12,
        reviewer="dm:test",
        observation="The creature card is visibly printed on the left side of the page.",
        metadata={"language": "en"},
    )
    replay = modules.review_content(
        campaign_id=campaign.id,
        module_id=imported.module_id,
        scene_id=scene["scene_id"],
        content_key="necromite-of-myrkul",
        content_kind="dnd5e_2014_statblock",
        normalized_content=markdown,
        source_asset_id=asset["id"],
        page_number=12,
        reviewer="dm:test",
        observation="The creature card is visibly printed on the left side of the page.",
        metadata={"language": "en"},
    )

    assert replay["id"] == reviewed["id"]
    assert reviewed["evidence"]["asset_checksum"] == "b" * 64
    assert modules.get_content_review(campaign.id, reviewed["id"])["normalized_content"] == markdown
    assert [item["id"] for item in modules.list_content_reviews(
        campaign.id, imported.module_id, content_kind="dnd5e_2014_statblock"
    )] == [reviewed["id"]]
