import hashlib

import pytest

from sagasmith_core import IdempotencyService, IdempotencyWrite
from sagasmith_core.campaigns import CampaignService
from sagasmith_core.documents import NormalizedDocument
from sagasmith_core.models import ModuleSource
from sagasmith_core.modules import (
    GenericModuleProfile,
    MarkdownModuleParser,
    ModuleService,
    SceneBoundary,
)
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
    expanded = service.expand(hits[0].id)
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
    assert expanded["content_sha256"] == hashlib.sha256(
        expanded["content"].encode("utf-8")
    ).hexdigest()
    assert expanded["source_ref"] == {
        "module_id": result.module_id,
        "scene_id": hits[0].metadata["scene_id"],
        "chunk_id": hits[0].id,
        "page_start": expanded["page_start"],
        "page_end": expanded["page_end"],
        "heading_path": expanded["heading_path"],
        "content_sha256": expanded["content_sha256"],
    }
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


def test_module_search_can_be_scoped_to_one_active_revision(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Revisions")
    service = ModuleService(database)
    old = service.ingest(
        campaign_id=campaign.id,
        source_key="adventure-v1",
        title="Adventure",
        content="# Chapter\n## Old Route\nshared evidence from the old revision",
    )
    current = service.ingest(
        campaign_id=campaign.id,
        source_key="adventure-v2",
        title="Adventure",
        content="# Chapter\n## Current Route\nshared evidence from the current revision",
    )

    unscoped = service.search(campaign_id=campaign.id, query="shared evidence", top_k=10)
    scoped = service.search(
        campaign_id=campaign.id,
        query="shared evidence",
        top_k=10,
        module_ids=[current.module_id],
    )

    assert {hit.source_id for hit in unscoped} == {old.module_id, current.module_id}
    assert {hit.source_id for hit in scoped} == {current.module_id}


def test_module_chunks_can_be_listed_in_source_order(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Chunk review")
    service = ModuleService(database)
    imported = service.ingest(
        campaign_id=campaign.id,
        source_key="creatures.md",
        title="Creatures",
        content=(
            "# Appendix B\n"
            "## Monsters\n"
            "##### Goblin\nSmall humanoid, neutral evil.\n"
            "##### Actions\nScimitar. Melee Weapon Attack.\n"
        ),
    )
    scene_id = service.scene_index(campaign.id)[0]["scene_id"]

    chunks = service.list_chunks(campaign.id, imported.module_id, scene_id=scene_id)

    assert [item["ordinal"] for item in chunks] == sorted(
        item["ordinal"] for item in chunks
    )
    assert all(item["module_id"] == imported.module_id for item in chunks)
    assert all(item["scene_id"] == scene_id for item in chunks)
    assert any(item["heading_path"][-1] == "Goblin" for item in chunks)
    assert any(item["heading_path"][-1] == "Actions" for item in chunks)


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


def test_module_parser_uses_global_page_offsets_for_same_page_chapters() -> None:
    chapters = MarkdownModuleParser().parse(
        "<!-- page: 1 -->\n# Chapter One\n## Arrival\nFirst.\n"
        "# Same-page Appendix\n## Card\nSecond.\n"
        "<!-- page: 2 -->\n# Chapter Two\n## Departure\nThird.\n"
    )

    assert [chapter.metadata["page_start"] for chapter in chapters] == [1, 1, 2]
    assert [chapter.metadata["page_end"] for chapter in chapters] == [1, 1, 2]
    assert [chapter.scenes[0].metadata["page_start"] for chapter in chapters] == [1, 1, 2]


def test_module_parser_excludes_page_marker_before_next_heading_from_chunk() -> None:
    chapters = MarkdownModuleParser().parse(
        "<!-- page: 204 -->\n"
        "# Appendix B\n"
        "## Monsters\n"
        "##### Gazer\n"
        "Tiny aberration.\n"
        "##### Actions\n"
        "Eye Rays. The gazer shoots two magical rays.\n"
        "<!-- page: 205 -->\n"
        "#### Hlam\n"
        "Medium humanoid.\n"
    )

    chunks = [
        chunk
        for chapter in chapters
        for scene in chapter.scenes
        for chunk in scene.chunks
    ]
    eye_rays = next(chunk for chunk in chunks if "Eye Rays" in chunk.content)
    hlam = next(chunk for chunk in chunks if "Medium humanoid" in chunk.content)

    assert eye_rays.metadata["page_start"] == 204
    assert eye_rays.metadata["page_end"] == 204
    assert hlam.metadata["page_start"] == 205
    assert "<!-- page: 205 -->" not in eye_rays.content


def test_module_parser_excludes_page_marker_before_next_scene_from_scene_end() -> None:
    chapters = MarkdownModuleParser().parse(
        "<!-- page: 1 -->\n"
        "# Chapter\n"
        "## Arrival\n"
        "First scene.\n"
        "<!-- page: 2 -->\n"
        "## Departure\n"
        "Second scene.\n"
    )

    assert chapters[0].scenes[0].metadata["page_end"] == 1
    assert chapters[0].scenes[1].metadata["page_start"] == 2


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


def test_module_preview_reuses_shared_document_cache(database, tmp_path) -> None:
    source = tmp_path / "module.md"
    source.write_text("# Chapter One\n\n## Arrival\n\nText.\n", encoding="utf-8")
    cache = tmp_path / "normalized-modules"
    service = ModuleService(database)

    first = service.preview_path(source, document_cache_dir=cache)
    second = service.preview_path(
        source,
        document_cache_dir=cache,
        expected_checksum=first["checksum"],
    )

    assert first["metadata"]["normalization_cache_hit"] is False
    assert second["metadata"]["normalization_cache_hit"] is True
    assert second["scenes"] == first["scenes"]


def test_module_profile_metadata_is_validated_persisted_and_listed(database, tmp_path) -> None:
    class ManifestProfile:
        name = "manifest-test"
        version = "1"

        def classify_chunk(self, heading: str, text: str) -> str:
            return "narrative"

        def keywords(self, title: str, text: str) -> list[str]:
            return []

        def scene_boundaries(self, chapter_title: str, chapter_content: str):
            return [SceneBoundary("Scene", 0, len(chapter_content))]

        def document_metadata(self, content: str) -> dict:
            return {"runtime_manifest": {"schema_version": 1, "module_key": "keep"}}

    source = tmp_path / "manifest.md"
    source.write_text("# Chapter\nBody.\n", encoding="utf-8")
    parser = MarkdownModuleParser(profile=ManifestProfile())
    preview = ModuleService(database).preview_path(source, parser=parser)
    assert preview["profile_metadata"]["runtime_manifest"]["module_key"] == "keep"

    campaign = CampaignService(database).create(system_id="dnd5e", name="Manifest")
    ModuleService(database).ingest_path(
        campaign_id=campaign.id,
        path=source,
        source_key="manifest",
        parser=parser,
    )
    listed = ModuleService(database).list(campaign.id)
    assert listed[0]["runtime_manifest"]["module_key"] == "keep"


def test_module_profile_metadata_errors_fail_preview_and_ingest(database, tmp_path) -> None:
    class InvalidManifestProfile(GenericModuleProfile):
        def document_metadata(self, content: str) -> dict:
            return {"runtime_manifest_errors": ["duplicate id: npc:keeper"]}

    source = tmp_path / "invalid-manifest.md"
    source.write_text("# Chapter\n## Scene\nBody.\n", encoding="utf-8")
    parser = MarkdownModuleParser(profile=InvalidManifestProfile())
    preview = ModuleService(database).preview_path(source, parser=parser)
    assert preview["valid"] is False
    assert preview["errors"] == ["duplicate id: npc:keeper"]

    campaign = CampaignService(database).create(system_id="dnd5e", name="Invalid")
    with pytest.raises(ValueError, match="invalid module runtime manifest"):
        ModuleService(database).ingest_path(
            campaign_id=campaign.id,
            path=source,
            source_key="invalid",
            parser=parser,
        )


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


def test_legacy_set_active_uses_the_candidate_activation_transaction(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="One activation path")
    service = ModuleService(database)
    current = service.ingest(
        campaign_id=campaign.id,
        source_key="module-v1",
        logical_source_key="module",
        title="Module",
        content="# Chapter\n## Opening\nOld.\n",
        activate=True,
    )
    candidate = service.ingest(
        campaign_id=campaign.id,
        source_key="module-v2",
        logical_source_key="module",
        title="Module",
        content="# Chapter\n## Opening\nNew.\n",
        activate=False,
    )

    activation = service.set_active(campaign.id, candidate.module_id, active=True)

    assert activation["replaced_module_ids"] == [current.module_id]
    visible = service.list(campaign.id)
    assert [item["id"] for item in visible] == [candidate.module_id]


def test_direct_active_ingest_uses_logical_key_activation_and_progress_migration(
    database,
) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Direct activation")
    service = ModuleService(database)
    current = service.ingest(
        campaign_id=campaign.id,
        source_key="module-v1.md",
        logical_source_key="module",
        title="Module",
        content="# Chapter\n## Opening\nOld.\n",
    )
    old_scene = service.scene_index(campaign.id, module_id=current.module_id)[0]
    service.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=old_scene["scene_id"],
        progress=45,
        state={"clue_found": True},
    )

    replacement = service.ingest(
        campaign_id=campaign.id,
        source_key="module-v2.md",
        logical_source_key="module",
        title="Module",
        content="# Chapter\n## Opening\nNew.\n",
        activate=True,
    )

    visible = service.list(campaign.id)
    assert [item["id"] for item in visible] == [replacement.module_id]
    current_scene = service.current_scene(campaign.id)
    assert current_scene is not None
    assert current_scene["module_id"] == replacement.module_id
    assert current_scene["progress"]["percent"] == 45
    assert current_scene["progress"]["state"] == {"clue_found": True}


def test_retired_module_scene_cannot_become_current(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Retired scene")
    service = ModuleService(database)
    retired = service.ingest(
        campaign_id=campaign.id,
        source_key="module-v1.md",
        logical_source_key="module",
        title="Module",
        content="# Chapter\n## Opening\nOld.\n",
    )
    retired_scene = service.scene_index(campaign.id, module_id=retired.module_id)[0]
    service.ingest(
        campaign_id=campaign.id,
        source_key="module-v2.md",
        logical_source_key="module",
        title="Module",
        content="# Chapter\n## Opening\nNew.\n",
    )

    with pytest.raises(ValueError, match="retired module revision"):
        service.set_scene_progress(
            campaign_id=campaign.id,
            scene_id=retired_scene["scene_id"],
            status="current",
        )


def test_module_candidate_activation_and_exact_receipt_commit_together(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Atomic module")
    service = ModuleService(database)
    result = service.ingest(
        campaign_id=campaign.id,
        source_key="module",
        logical_source_key="module",
        title="Module",
        content="# Chapter\nBody.\n",
        activate=False,
    )
    payload = {"module_id": result.module_id}

    activation = service.activate_candidate(
        campaign.id,
        result.module_id,
        idempotency_key="activate",
        idempotency_write=IdempotencyWrite(
            scope=f"module-activation:{campaign.id}",
            payload=payload,
            response=lambda value: {"activation": value},
        ),
    )

    replay = IdempotencyService(database).lookup(
        f"module-activation:{campaign.id}",
        "activate",
        payload,
    )
    assert replay is not None
    assert replay.response == {"activation": activation}


def test_module_candidate_activation_rolls_back_when_receipt_fails(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Module rollback")
    service = ModuleService(database)
    result = service.ingest(
        campaign_id=campaign.id,
        source_key="module",
        logical_source_key="module",
        title="Module",
        content="# Chapter\nBody.\n",
        activate=False,
    )

    with pytest.raises(RuntimeError, match="receipt failed"):
        service.activate_candidate(
            campaign.id,
            result.module_id,
            idempotency_key="activate",
            idempotency_write=IdempotencyWrite(
                scope=f"module-activation:{campaign.id}",
                payload={"module_id": result.module_id},
                response=lambda _value: (_ for _ in ()).throw(
                    RuntimeError("receipt failed")
                ),
            ),
        )

    with database.transaction() as session:
        assert session.get(ModuleSource, result.module_id).active is False


def test_module_activation_remaps_progress_by_stable_scene_identity(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Module remap")
    service = ModuleService(database)
    old = service.ingest(
        campaign_id=campaign.id,
        source_key="module",
        logical_source_key="module",
        title="Module",
        content="# Chapter\n## Cave\nOld text.\n",
    )
    old_scene = service.scene_index(campaign.id, module_id=old.module_id)[0]
    service.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=old_scene["scene_id"],
        progress=45,
        state={"door_open": True},
    )
    candidate = service.ingest(
        campaign_id=campaign.id,
        source_key="module",
        logical_source_key="module",
        title="Module",
        content="# Chapter\n## Cave\nReparsed text.\n",
        activate=False,
    )
    new_scene = service.scene_index(campaign.id, module_id=candidate.module_id)[0]

    activation = service.activate_candidate(campaign.id, candidate.module_id)

    assert activation["progress_migrations"] == [
        {
            "scope_id": "party",
            "from_scene_id": old_scene["scene_id"],
            "to_scene_id": new_scene["scene_id"],
            "stable_key": "chapter-cave",
            "mode": "stable_key",
        }
    ]
    current = service.current_scene(campaign.id)
    assert current is not None
    assert current["module_id"] == candidate.module_id
    assert current["scene_id"] == new_scene["scene_id"]
    assert current["progress"]["percent"] == 45
    assert current["progress"]["state"] == {"door_open": True}


def test_module_activation_requires_explicit_ruling_for_removed_progress(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Module ruling")
    service = ModuleService(database)
    old = service.ingest(
        campaign_id=campaign.id,
        source_key="module",
        logical_source_key="module",
        title="Module",
        content="# Chapter\n## Removed Route\nOld text.\n",
    )
    old_scene = service.scene_index(campaign.id, module_id=old.module_id)[0]
    service.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=old_scene["scene_id"],
        progress=45,
    )
    candidate = service.ingest(
        campaign_id=campaign.id,
        source_key="module",
        logical_source_key="module",
        title="Module",
        content="# Chapter\n## Replacement Route\nNew text.\n",
        activate=False,
    )
    new_scene = service.scene_index(campaign.id, module_id=candidate.module_id)[0]

    with pytest.raises(ValueError, match="DM-reviewed progress remap"):
        service.activate_candidate(campaign.id, candidate.module_id)

    assert service.current_scene(campaign.id)["module_id"] == old.module_id
    activation = service.activate_candidate(
        campaign.id,
        candidate.module_id,
        progress_remaps={old_scene["scene_id"]: new_scene["scene_id"]},
    )
    assert activation["progress_migrations"][0]["mode"] == "dm_ruling"
    assert service.current_scene(campaign.id)["scene_id"] == new_scene["scene_id"]


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

    candidate = modules.ingest(
        campaign_id=campaign.id,
        source_key="keep.md",
        logical_source_key="keep.md",
        title="The Keep",
        content="# Chapter\n## Courtyard\nThe revised entry.",
        activate=False,
    )
    courtyard = modules.scene_index(campaign.id, module_id=candidate.module_id)[0]
    modules.activate_candidate(
        campaign.id,
        candidate.module_id,
        progress_remaps={original["scene_id"]: courtyard["scene_id"]},
    )

    assert [item["title"] for item in modules.scene_index(campaign.id)] == ["Courtyard"]
    assert [
        item["title"]
        for item in modules.scene_index(
            campaign.id,
            module_id=original["module_id"],
        )
    ] == ["Gate"]
    assert modules.current_scene(campaign.id)["title"] == "Courtyard"
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


def test_text_module_content_review_keeps_exact_chunk_evidence(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Text review")
    modules = ModuleService(database)
    imported = modules.ingest(
        campaign_id=campaign.id,
        source_key="monsters.md",
        title="Monsters",
        content=(
            "<!-- page: 8 -->\n# Appendix B\n## Goblin\n"
            "Small humanoid, neutral evil Armor Class 15 Hit Points 7 Speed 30 ft.\n"
            "##### Actions\nScimitar. Melee Weapon Attack.\n"
        ),
    )
    scene = modules.scene_index(campaign.id)[0]
    chunks = modules.list_chunks(
        campaign.id, imported.module_id, scene_id=scene["scene_id"]
    )

    reviewed = modules.review_content(
        campaign_id=campaign.id,
        module_id=imported.module_id,
        scene_id=scene["scene_id"],
        content_key="goblin",
        content_kind="dnd5e_2014_statblock",
        normalized_content="# Goblin\n\n*Small humanoid, neutral evil*",
        source_chunk_ids=[item["id"] for item in chunks],
        reviewer="dm:test",
        observation="Reviewed the normalized text against every source chunk.",
    )

    assert reviewed["evidence"]["confidence"] == "reviewed_text"
    assert reviewed["evidence"]["source_chunk_ids"] == [
        item["id"] for item in chunks
    ]
    assert reviewed["evidence"]["page_start"] == 8
    assert reviewed["evidence"]["page_end"] == 8


def test_module_domain_writes_persist_exact_receipts_atomically(
    database,
    tmp_path,
) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Atomic module writes",
    )
    modules = ModuleService(database)
    idempotency = IdempotencyService(database)
    ingest_payload = {"source_key": "atomic.md"}
    imported = modules.ingest(
        campaign_id=campaign.id,
        source_key="atomic.md",
        title="Atomic",
        content="# Chapter\n## Scene\nExact evidence.",
        idempotency_key="module-ingest",
        idempotency_write=IdempotencyWrite(
            scope=f"module-ingest:{campaign.id}:dm:test",
            payload=ingest_payload,
            response=lambda result: {"module_id": result.module_id},
        ),
    )
    assert idempotency.lookup(
        f"module-ingest:{campaign.id}:dm:test",
        "module-ingest",
        ingest_payload,
    ).response == {"module_id": imported.module_id}

    asset_path = tmp_path / "map.png"
    asset_path.write_bytes(b"map")

    def fail_response(_result):
        raise RuntimeError("asset response failed")

    with pytest.raises(RuntimeError, match="asset response failed"):
        modules.register_asset(
            campaign_id=campaign.id,
            module_id=imported.module_id,
            source_path=str(asset_path),
            media_type="image/png",
            checksum="a" * 64,
            idempotency_key="asset-failure",
            idempotency_write=IdempotencyWrite(
                scope=f"module-asset:{campaign.id}:dm:test",
                payload={"path": str(asset_path)},
                response=fail_response,
            ),
        )
    assert modules.list_assets(campaign.id, imported.module_id) == []

    scene = modules.scene_index(campaign.id)[0]
    chunks = modules.list_chunks(
        campaign.id,
        imported.module_id,
        scene_id=scene["scene_id"],
    )

    def fail_review_response(_result):
        raise RuntimeError("review response failed")

    with pytest.raises(RuntimeError, match="review response failed"):
        modules.review_content(
            campaign_id=campaign.id,
            module_id=imported.module_id,
            scene_id=scene["scene_id"],
            content_key="atomic-card",
            content_kind="statblock",
            normalized_content="# Atomic Card",
            source_chunk_ids=[item["id"] for item in chunks],
            reviewer="dm:test",
            observation="Reviewed against the exact indexed source chunks.",
            idempotency_key="review-failure",
            idempotency_write=IdempotencyWrite(
                scope=f"module-review:{campaign.id}:dm:test",
                payload={"content_key": "atomic-card"},
                response=fail_review_response,
            ),
        )
    assert modules.list_content_reviews(campaign.id, imported.module_id) == []
