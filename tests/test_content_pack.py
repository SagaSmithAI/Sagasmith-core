from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from sagasmith_core.content_pack import (
    ContentPackageError,
    blob_descriptor,
    build_actor_card,
    build_content_package,
    build_source_bundle,
    dumps_content_archive,
    loads_content_archive,
    source_ref,
)
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
            "selection_rules": [],
            "resolutions": [],
        },
        metadata={"distribution": "shareable"},
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
