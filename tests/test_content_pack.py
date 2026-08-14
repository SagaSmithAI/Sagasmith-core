from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from sagasmith_core.campaigns import CampaignService
from sagasmith_core.characters import CharacterService
from sagasmith_core.content_pack import (
    ContentPackageError,
    blob_descriptor,
    build_actor_card,
    build_content_package,
    build_source_bundle,
    content_package_checksum,
    dumps_content_archive,
    loads_content_archive,
    source_ref,
    validate_content_package,
)
from sagasmith_core.modules import ModuleService
from sagasmith_core.rules import RuleService


def _package() -> tuple[dict, dict[str, bytes]]:
    document = "# Rules\nA luminous ward protects one ally."
    source, document_asset, document_blob = build_source_bundle(
        source_key="example.rules",
        title="Example Rules",
        normalized_text=document,
        edition="2014",
        sections=[
            {
                "ordinal": 0,
                "parent_ordinal": None,
                "level": 1,
                "title": "Rules",
                "path": ["Rules"],
                "start_offset": 0,
                "end_offset": len(document),
                "chunks": [
                    {
                        "ordinal": 0,
                        "heading_path": ["Rules"],
                        "start_offset": 0,
                        "end_offset": len(document),
                        "token_count": 7,
                        "page_start": 1,
                        "page_end": 1,
                        "metadata": {},
                    }
                ],
            }
        ],
        license="CC-BY-4.0",
        attribution="Example author",
    )
    image = b"\x89PNG\r\n\x1a\nexample"
    image_asset = blob_descriptor(
        asset_key="actor.scout.image",
        kind="actor_image",
        name="scout.png",
        media_type="image/png",
        content=image,
        license="CC-BY-4.0",
        attribution="Example artist",
    )
    chunk = source["sections"][0]["chunks"][0]
    citation = source_ref(
        source_key=source["source_key"],
        chunk_key=chunk["key"],
        page=1,
        note="Feature evidence",
    )
    actor = build_actor_card(
        actor_id="example.actor.scout",
        version="1.0.0",
        system_id="dnd5e",
        actor_type="npc",
        name="Scout",
        sheet={"schema_version": 2},
        notes={},
        provenance={"source_refs": [citation]},
        image_asset_key=image_asset["asset_key"],
        image_alt="Scout portrait",
    )
    package = build_content_package(
        kind="addon",
        package_id="example.addon",
        version="1.0.0",
        system_id="dnd5e",
        manifest={"title": "Example Addon"},
        sources=[source],
        assets=[document_asset, image_asset],
        content_reviews=[],
        actors=[actor],
        content={
            "classification": "third_party",
            "editions": ["2014"],
            "activation": {"rule_policy": "branch"},
            "conflicts": [],
            "rule_definitions": [],
            "artifacts": [{"id": "ward", "source_refs": [citation]}],
            "mechanics": [],
        },
        metadata={"distribution": "shareable"},
    )
    return package, {
        document_asset["checksum"]: document_blob,
        image_asset["checksum"]: image,
    }


def test_image_bearing_content_actor_imports_with_package_assets(database) -> None:
    package, _blobs = _package()
    assets_by_key = {item["asset_key"]: item for item in package["assets"]}
    campaign = CampaignService(database).create(system_id="dnd5e", name="Actor import")
    characters = CharacterService(database)

    template = characters.import_content_actor(
        package["actors"][0],
        assets_by_key=assets_by_key,
    )
    instance = characters.instantiate(template.id, campaign_id=campaign.id)

    assert template.campaign_id is None
    assert instance.template_id == template.id
    assert instance.campaign_id == campaign.id
    assert instance.sheet == package["actors"][0]["sheet"]


def _module_package() -> tuple[dict, dict[str, bytes]]:
    document = "# Chapter\n## Scene\nA private promise changes the road."
    source, document_asset, document_blob = build_source_bundle(
        source_key="example.module",
        title="Example Module",
        normalized_text=document,
        sections=[
            {
                "ordinal": 0,
                "parent_ordinal": None,
                "level": 1,
                "title": "Chapter",
                "path": ["Chapter"],
                "start_offset": 0,
                "end_offset": len(document),
                "chunks": [
                    {
                        "ordinal": 0,
                        "heading_path": ["Chapter", "Scene"],
                        "start_offset": 0,
                        "end_offset": len(document),
                        "token_count": 10,
                        "page_start": 1,
                        "page_end": 1,
                        "metadata": {},
                    }
                ],
            }
        ],
        license="Apache-2.0",
        attribution="Example author",
    )
    image = b"\x89PNG\r\n\x1a\nmap"
    image_asset = blob_descriptor(
        asset_key="example.module.map",
        kind="map",
        name="map.png",
        media_type="image/png",
        content=image,
        license="Apache-2.0",
        attribution="Example author",
    )
    chunk = source["sections"][0]["chunks"][0]
    citation = source_ref(
        source_key=source["source_key"],
        chunk_key=chunk["key"],
        page=1,
        note="Scene evidence",
    )
    package = build_content_package(
        kind="module",
        package_id="example.module",
        version="1.0.0",
        system_id="neutral",
        manifest={"title": "Example Module"},
        sources=[source],
        assets=[document_asset, image_asset],
        content_reviews=[
            {
                "id": "review.scene.promise",
                "kind": "narrative",
                "status": "accepted",
                "target": {"scene_key": "scene.promise", "content_key": "promise"},
                "normalized_content": "A private promise changes the road.",
                "evidence": {"asset_key": None, "page": None},
                "source_refs": [citation],
                "review": {"reviewer": "agent:test", "observation": "Exact source text."},
                "metadata": {},
            }
        ],
        actors=[],
        content={
            "classification": "original",
            "compatibility": {},
            "play_profile": {},
            "continuity": {},
            "activation": {"mode": "campaign_attach"},
            "scene_atlas": [
                {
                    "stable_key": "scene.promise",
                    "title": "Scene",
                    "chapter": "Chapter",
                    "chapter_ordinal": 0,
                    "scene_ordinal": 0,
                    "scene_type": "scene",
                    "page_start": 1,
                    "page_end": 1,
                    "headings": ["Chapter", "Scene"],
                    "keywords": [],
                    "metadata": {},
                    "source_span": {
                        "source_key": source["source_key"],
                        "start_offset": 0,
                        "end_offset": len(document),
                    },
                    "source_refs": [citation],
                }
            ],
            "catalogs": {},
            "narrative": {},
        },
        metadata={
            "agent_finalization": {
                "confirmed": True,
                "reviewer": "agent:test",
                "note": "Reviewed against the original source.",
            }
        },
    )
    return package, {
        document_asset["checksum"]: document_blob,
        image_asset["checksum"]: image,
    }


def test_source_bundle_keys_identical_text_by_structure_and_hash() -> None:
    source, _asset, _blob = build_source_bundle(
        source_key="example.duplicate-chunks",
        title="Duplicate Chunks",
        normalized_text="# Empty slots",
        sections=[
            {
                "ordinal": 4,
                "parent_ordinal": None,
                "level": 1,
                "title": "Empty slots",
                "path": ["Empty slots"],
                "start_offset": 0,
                "end_offset": 13,
                "chunks": [
                    {
                        "ordinal": ordinal,
                        "heading_path": ["Empty slots"],
                        "start_offset": 13,
                        "end_offset": 13,
                        "token_count": 0,
                        "page_start": 1,
                        "page_end": 1,
                        "metadata": {},
                    }
                    for ordinal in (7, 8)
                ],
            }
        ],
    )
    chunks = source["sections"][0]["chunks"]
    assert chunks[0]["content_hash"] == chunks[1]["content_hash"]
    assert chunks[0]["key"] != chunks[1]["key"]
    assert chunks[0]["key"].endswith("chunk-7-e3b0c44298fc1c14")
    assert chunks[1]["key"].endswith("chunk-8-e3b0c44298fc1c14")


def test_content_archive_round_trip_uses_one_source_blob_and_external_actor_image() -> None:
    package, blobs = _package()
    assert package["manifest"] == {
        "title": "Example Addon",
        "id": package["id"],
        "version": package["version"],
        "system_id": package["system_id"],
    }
    archive = dumps_content_archive(package, blobs)
    loaded, loaded_blobs = loads_content_archive(archive)
    assert loaded == package
    assert loaded_blobs == blobs
    assert "content" not in loaded["sources"][0]["sections"][0]
    assert loaded["actors"][0]["image"] == {
        "asset_key": "actor.scout.image",
        "alt": "Scout portrait",
    }
    assert loaded["schema_version"] == 2
    assert "readiness" not in loaded


def test_content_package_requires_manifest_identity() -> None:
    package, _ = _package()
    del package["manifest"]["id"]
    unsigned = {key: value for key, value in package.items() if key != "checksum"}
    from sagasmith_core.integrity import canonical_json

    package["checksum"] = hashlib.sha256(canonical_json(unsigned).encode()).hexdigest()
    from sagasmith_core.content_pack import validate_content_package

    with pytest.raises(ContentPackageError, match="manifest.id"):
        validate_content_package(package)


def test_content_package_rejects_legacy_readiness_field() -> None:
    from sagasmith_core.content_pack import validate_content_package
    from sagasmith_core.integrity import canonical_json

    package, _ = _package()
    package["readiness"] = {"complete": True}
    unsigned = {key: value for key, value in package.items() if key != "checksum"}
    package["checksum"] = hashlib.sha256(canonical_json(unsigned).encode()).hexdigest()
    with pytest.raises(ContentPackageError, match="unsupported fields: readiness"):
        validate_content_package(package)


def test_content_archive_rejects_changed_source_slice_and_extra_files() -> None:
    package, blobs = _package()
    document_asset = package["assets"][0]
    changed = dict(blobs)
    changed[document_asset["checksum"]] = b"# Rules\nInvented text"
    with pytest.raises(ContentPackageError, match="archive blob mismatch"):
        dumps_content_archive(package, changed)

    archive = dumps_content_archive(package, blobs)
    damaged = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(damaged, "w") as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
        target.writestr("legacy.json", b"{}")
    with pytest.raises(ContentPackageError, match="unsupported paths"):
        loads_content_archive(damaged.getvalue())


def test_content_package_rejects_unresolved_citation() -> None:
    package, _ = _package()
    package["content"]["artifacts"][0]["source_refs"][0]["chunk_key"] = "missing"
    unsigned = {key: value for key, value in package.items() if key != "checksum"}
    from sagasmith_core.integrity import canonical_json

    package["checksum"] = hashlib.sha256(canonical_json(unsigned).encode()).hexdigest()
    with pytest.raises(ContentPackageError, match="does not resolve"):
        from sagasmith_core.content_pack import validate_content_package

        validate_content_package(package)


def test_rule_service_exports_one_normalized_document_blob(database) -> None:
    rules = RuleService(database)
    imported = rules.ingest(
        system_id="dnd5e",
        source_key="example.rule-source",
        title="Rule Source",
        content="# First\nAlpha.\n\n## Second\nBeta.",
        edition="2014",
    )
    source, asset, blob = rules.export_content_source(imported.source_id)
    assert source["normalized_document_asset_key"] == asset["asset_key"]
    assert hashlib.sha256(blob).hexdigest() == asset["checksum"]
    assert all("content" not in section for section in source["sections"])
    assert all(
        "content" not in chunk for section in source["sections"] for chunk in section["chunks"]
    )


def test_rule_service_imports_unified_source_and_returns_unified_chunk_keys(database) -> None:
    package, blobs = _package()
    source = package["sources"][0]
    asset = next(
        item
        for item in package["assets"]
        if item["asset_key"] == source["normalized_document_asset_key"]
    )
    imported = RuleService(database).import_content_source(
        source,
        blobs[asset["checksum"]],
        system_id="dnd5e",
    )
    chunk_key = source["sections"][0]["chunks"][0]["key"]
    assert imported["source_id"]
    assert imported["chunk_map"][chunk_key]


def test_manifest_identity_cannot_disagree_with_package_identity() -> None:
    package, _blobs = _package()
    package["manifest"]["id"] = "different.addon"
    unsigned = {key: value for key, value in package.items() if key != "checksum"}
    from sagasmith_core.integrity import canonical_json

    package["checksum"] = hashlib.sha256(canonical_json(unsigned).encode()).hexdigest()
    with pytest.raises(ContentPackageError, match="manifest.id"):
        from sagasmith_core.content_pack import validate_content_package

        validate_content_package(package)


def test_module_package_requires_agent_finalization_and_complete_review_shape() -> None:
    from sagasmith_core.content_pack import validate_content_package

    package, _ = _module_package()
    del package["metadata"]["agent_finalization"]
    package["checksum"] = content_package_checksum(package)
    with pytest.raises(ContentPackageError, match="agent_finalization"):
        validate_content_package(package)

    package, _ = _module_package()
    del package["content_reviews"][0]["target"]
    package["checksum"] = content_package_checksum(package)
    with pytest.raises(ContentPackageError, match="missing fields: target"):
        validate_content_package(package)


def test_non_module_review_payload_remains_system_owned() -> None:
    from sagasmith_core.content_pack import validate_content_package

    package, _ = _package()
    package["content_reviews"] = [
        {
            "system_review_kind": "artifact_ruling",
            "decision": {"accepted": True},
            "source_refs": [package["content"]["artifacts"][0]["source_refs"][0]],
        }
    ]
    package["checksum"] = content_package_checksum(package)

    assert validate_content_package(package)["content_reviews"] == package["content_reviews"]


def test_module_package_database_import_rolls_back_as_one_transaction(database) -> None:
    package, blobs = _module_package()
    campaign = CampaignService(database).create(system_id="neutral", name="Atomic Pack")

    def fail_asset_write(_module_id: str, _asset: dict, _content: bytes) -> str:
        raise RuntimeError("asset staging failed")

    with pytest.raises(RuntimeError, match="asset staging failed"):
        ModuleService(database).import_content_package(
            campaign.id,
            package,
            blobs,
            asset_writer=fail_asset_write,
        )

    assert ModuleService(database).list(campaign.id, include_retired=True) == []


def test_module_package_import_restores_profile_data_without_nesting(database, tmp_path) -> None:
    package, blobs = _module_package()
    package["content"]["scene_atlas"][0]["metadata"] = {
        "visibility": "restricted",
        "profile_data": {"stress": [{"loss": "1/1d4"}]},
    }
    package["checksum"] = content_package_checksum(package)
    campaign = CampaignService(database).create(system_id="neutral", name="Profile data")

    imported = ModuleService(database).import_content_package(
        campaign.id,
        package,
        blobs,
        asset_writer=lambda _module_id, asset, _content: str(tmp_path / asset["name"]),
    )
    scene = ModuleService(database).scene_index(
        campaign.id,
        module_id=imported["module_id"],
    )[0]

    assert scene["visibility"] == "restricted"
    assert scene["profile_data"]["stress"] == [{"loss": "1/1d4"}]
    assert "profile_data" not in scene["profile_data"]


def test_module_package_rejects_profile_fields_outside_profile_data() -> None:
    package, _blobs = _module_package()
    package["content"]["scene_atlas"][0]["metadata"] = {
        "visibility": "restricted",
        "stress": [{"loss": "1/1d4"}],
    }
    package["checksum"] = content_package_checksum(package)

    with pytest.raises(ContentPackageError, match="outside profile_data: stress"):
        validate_content_package(package)
