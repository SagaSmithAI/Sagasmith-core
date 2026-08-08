import base64
import copy
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import delete

from sagasmith_core.campaigns import CampaignService
from sagasmith_core.characters import CharacterService
from sagasmith_core.models import CampaignSnapshot, ModuleChunk, ModuleScene
from sagasmith_core.modules import ModuleService
from sagasmith_core.portable import (
    PortableContentError,
    build_actor_card,
    build_addon_pack,
    build_preset_pack,
    build_release_manifest,
    build_rule_pack,
    dumps_portable,
    loads_portable,
    portable_rule_definition_checksum,
    validate_actor_card,
    validate_addon_pack,
    validate_addon_readiness,
    validate_release_manifest,
    validate_rule_pack,
)
from sagasmith_core.rules import RuleService
from sagasmith_core.snapshots import SnapshotService


def _card() -> dict:
    return build_actor_card(
        portable_id="example.goblin-scout",
        version="1.0.0",
        system_id="dnd5e",
        actor_type="monster",
        name="Goblin Scout",
        summary="A portable example.",
        sheet={"schema_version": 2, "hp": 7},
        notes={"schema_version": 2, "profile": {"summary": "A wary scout."}},
        provenance={"source_refs": [{"document": "srd", "page": 123}]},
        bindings=[
            {
                "kind": "module_scene",
                "module_key": "example.adventure",
                "scene_key": "chapter-1/ambush",
                "role": "hostile",
            }
        ],
        metadata={"license": "CC-BY-4.0"},
    )


def test_actor_card_is_deterministic_and_tamper_evident() -> None:
    card = _card()

    assert loads_portable(dumps_portable(card)) == card
    assert _card()["checksum"] == card["checksum"]

    tampered = copy.deepcopy(card)
    tampered["payload"]["sheet"]["hp"] = 99
    with pytest.raises(PortableContentError, match="checksum mismatch"):
        validate_actor_card(tampered)


def test_character_portable_round_trip_creates_fresh_runtime_identity(database) -> None:
    campaigns = CampaignService(database)
    characters = CharacterService(database)
    source_campaign = campaigns.create(system_id="dnd5e", name="Source")
    target_campaign = campaigns.create(system_id="dnd5e", name="Target")
    source = characters.create(
        system_id="dnd5e",
        campaign_id=source_campaign.id,
        character_type="npc",
        name="Mira",
        player_name="Source player",
        summary="Guide",
        sheet={"schema_version": 2, "hp": 12},
        notes={"schema_version": 2, "profile": {"summary": "Knows the old road."}},
    )

    image_bytes = b"\x89PNG\r\n\x1a\nportable-test-image"
    image = {
        "media_type": "image/png",
        "data_base64": base64.b64encode(image_bytes).decode("ascii"),
        "checksum": hashlib.sha256(image_bytes).hexdigest(),
        "size": len(image_bytes),
        "alt": "Portrait of Mira",
        "license": "CC0-1.0",
        "attribution": "Example fixture",
        "source_ref": "fixture:mira.png",
    }
    card = characters.export_portable_card(
        source.id,
        portable_id="example.mira",
        image=image,
        bindings=[
            {
                "kind": "module_scene",
                "module_key": "example.road",
                "scene_key": "arrival",
            }
        ],
    )
    imported = characters.import_portable_card(card, campaign_id=target_campaign.id)

    assert imported.id != source.id
    assert imported.campaign_id == target_campaign.id
    assert imported.template_id is None
    assert imported.revision == 1
    assert imported.sheet == source.sheet
    assert imported.notes == source.notes
    assert "campaign_id" not in card["payload"]
    assert "revision" not in card["payload"]
    assert "id" not in card["payload"]
    assert card["payload"]["image"] == image

    snapshot = SnapshotService(database).create(target_campaign.id, label="Imported actor")
    with database.transaction() as session:
        stored = session.get(CampaignSnapshot, snapshot.id)
        serialized = str(stored.payload)
    assert image["checksum"] not in serialized
    assert image["data_base64"] not in serialized


def test_actor_card_image_is_strictly_validated() -> None:
    card = _card()
    card["payload"]["image"] = {
        "media_type": "image/svg+xml",
        "data_base64": base64.b64encode(b"<svg/>").decode("ascii"),
        "checksum": hashlib.sha256(b"<svg/>").hexdigest(),
        "size": 6,
        "alt": "unsafe",
        "license": "CC0-1.0",
        "attribution": "fixture",
        "source_ref": "fixture:unsafe.svg",
    }
    from sagasmith_core.portable import portable_checksum

    card["checksum"] = portable_checksum(card)
    with pytest.raises(PortableContentError, match="media_type"):
        validate_actor_card(card)


def test_legacy_actor_card_remains_importable() -> None:
    card = _card()
    card["payload"]["card_schema"] = "sagasmith.actor-card.v1"
    card["payload"].pop("image")
    from sagasmith_core.portable import portable_checksum

    card["checksum"] = portable_checksum(card)
    assert validate_actor_card(card)["payload"]["card_schema"].endswith(".v1")


def test_actor_card_rejects_unstable_database_binding() -> None:
    card = _card()
    card["payload"]["bindings"] = [
        {
            "kind": "module_scene",
            "module_key": "example",
            "scene_key": "arrival",
            "scene_id": "database-id",
        }
    ]
    # Re-sign to prove semantic validation, rather than checksum validation, rejects it.
    from sagasmith_core.portable import portable_checksum

    card["checksum"] = portable_checksum(card)
    with pytest.raises(PortableContentError, match="unsupported fields"):
        validate_actor_card(card)


def test_rule_pack_round_trip_rehydrates_sources_and_uses_stable_chunk_keys(
    database,
) -> None:
    rules = RuleService(database)
    ingested = rules.ingest(
        system_id="dnd5e",
        source_key="example.extension",
        title="Example Extension",
        content="# Features\nA hero can learn the luminous ward.\n\n## Ward\nMark one foe.",
        edition="2014",
        version="1.0.0",
        publication_id="example.extension",
        authority="supplement",
    )
    source = rules.export_portable_source(ingested.source_id)
    chunk = source["sections"][0]["chunks"][0]
    stable_chunks = [item["key"] for section in source["sections"] for item in section["chunks"]]
    assert len(stable_chunks) == 2
    assert len(set(stable_chunks)) == 2
    assert [item["section_ordinal"] for item in rules.source_chunks(ingested.source_id)] == [
        0,
        1,
    ]
    package = build_rule_pack(
        portable_id="dnd5e.example-extension",
        version="1.0.0",
        system_id="dnd5e",
        manifest={
            "id": "dnd5e.example-extension",
            "version": "1.0.0",
            "title": "Example Extension",
            "namespace": "dnd5e.example-extension",
            "system_id": "dnd5e",
            "editions": ["2014"],
            "dependencies": [],
            "conflicts": [],
            "capabilities": [],
        },
        artifacts=[
            {
                "id": "dnd5e.example-extension.feature.ward",
                "kind": "feature",
                "card": {"name": "Luminous Ward"},
                "source_citations": [
                    {
                        "source": "rule-source:example.extension",
                        "source_key": "example.extension",
                        "chunk_key": chunk["key"],
                        "source_checksum": source["checksum"],
                    }
                ],
            }
        ],
        mechanics=[],
        provenance={"distribution": "private"},
        sources=[source],
        metadata={"license": "user-supplied", "distribution": "private"},
    )

    assert validate_rule_pack(package)["checksum"] == package["checksum"]
    assert portable_rule_definition_checksum(package) == package["metadata"][
        "definition_checksum"
    ]
    redistributed = copy.deepcopy(package)
    redistributed["metadata"]["distribution"] = "shareable"
    redistributed["metadata"]["license"] = "Apache-2.0"
    from sagasmith_core.portable import portable_checksum

    redistributed["checksum"] = portable_checksum(redistributed)
    assert validate_rule_pack(redistributed)["checksum"] != package["checksum"]
    assert portable_rule_definition_checksum(redistributed) == portable_rule_definition_checksum(
        package
    )
    imported = rules.import_portable_source(source, system_id="dnd5e")
    assert imported["skipped"] is True
    assert imported["chunk_map"][chunk["key"]]
    target = rules.import_portable_source(source, system_id="dnd5e-target")
    replay = rules.import_portable_source(source, system_id="dnd5e-target")
    assert target["skipped"] is False
    assert replay["skipped"] is True
    assert target["source_id"] != ingested.source_id
    assert target["chunk_map"] == replay["chunk_map"]
    assert set(target["chunk_map"]) == set(stable_chunks)
    assert "source_id" not in dumps_portable(package)
    assert "chunk_id" not in dumps_portable(package)

    tampered = copy.deepcopy(package)
    tampered["payload"]["sources"][0]["sections"][0]["chunks"][0]["content"] = "Invented text"
    tampered["checksum"] = portable_checksum(tampered)
    with pytest.raises(PortableContentError, match="content_hash mismatch"):
        validate_rule_pack(tampered)

    wrong_owner = copy.deepcopy(package)
    wrong_owner["payload"]["artifacts"][0]["source_citations"][0]["source_key"] = "missing.source"
    wrong_owner["checksum"] = portable_checksum(wrong_owner)
    with pytest.raises(PortableContentError, match="does not own its chunk_key"):
        validate_rule_pack(wrong_owner)

    leaked_id = copy.deepcopy(package)
    leaked_id["payload"]["sources"][0]["metadata"]["source_id"] = "local-id"
    leaked_id["checksum"] = portable_checksum(leaked_id)
    with pytest.raises(PortableContentError, match="machine-local fields|runtime locator fields"):
        validate_rule_pack(leaked_id)

    source_less = copy.deepcopy(package)
    source_less["payload"]["sources"] = []
    source_less["checksum"] = portable_checksum(source_less)
    with pytest.raises(PortableContentError, match="non-empty array"):
        validate_rule_pack(source_less)

    empty_path = copy.deepcopy(package)
    empty_path["payload"]["sources"][0]["sections"][0]["path"] = []
    empty_path["checksum"] = portable_checksum(empty_path)
    with pytest.raises(PortableContentError, match="non-empty string array"):
        validate_rule_pack(empty_path)

    wrong_dependency_kind = copy.deepcopy(package)
    wrong_dependency_kind["dependencies"] = [
        {
            "kind": "preset_pack",
            "id": "dnd5e.example-presets",
            "version": "1.0.0",
            "checksum": "a" * 64,
            "optional": False,
        }
    ]
    wrong_dependency_kind["checksum"] = portable_checksum(wrong_dependency_kind)
    with pytest.raises(PortableContentError, match="kind='rule_pack'"):
        validate_rule_pack(wrong_dependency_kind)


def test_release_manifest_composes_packages_without_embedding_them() -> None:
    release = build_release_manifest(
        portable_id="dnd5e.example-release",
        version="1.0.0",
        system_id="dnd5e",
        components=[
            {
                "kind": "rule_pack",
                "id": "dnd5e.example-rules",
                "version": "1.0.0",
                "checksum": "a" * 64,
                "optional": False,
            },
            {
                "kind": "preset_pack",
                "id": "dnd5e.example-presets",
                "version": "1.0.0",
                "checksum": "b" * 64,
                "optional": True,
            },
        ],
        metadata={"title": "Example release"},
    )

    assert validate_release_manifest(release) == release
    assert release["payload"] == {"release_schema": "sagasmith.release-manifest.v1"}


def test_addon_pack_embeds_exact_components_without_granting_authority() -> None:
    preset = build_preset_pack(
        portable_id="dnd5e.example-presets",
        version="1.0.0",
        system_id="dnd5e",
        cards=[_card()],
        metadata={"license": "CC-BY-4.0"},
    )
    addon = build_addon_pack(
        portable_id="dnd5e.example-addon",
        version="1.0.0",
        system_id="dnd5e",
        manifest={
            "id": "dnd5e.example-addon",
            "version": "1.0.0",
            "system_id": "dnd5e",
            "title": "Example Addon",
            "editions": ["2014"],
            "classification": "third_party",
            "content_summary": {"actor_card": 1},
            "activation": {
                "rule_policy": "none",
                "preset_policy": "library",
                "module_policy": "none",
            },
        },
        components=[preset],
        metadata={
            "distribution": "shareable",
            "license": "CC-BY-4.0",
            "attribution": "Example author",
        },
    )

    assert validate_addon_pack(addon, expected_system_id="dnd5e") == addon
    assert addon["payload"]["components"] == [preset]
    assert addon["dependencies"] == [
        {
            "kind": "preset_pack",
            "id": preset["id"],
            "version": preset["version"],
            "checksum": preset["checksum"],
            "optional": False,
        }
    ]

    tampered = copy.deepcopy(addon)
    tampered["payload"]["components"][0]["payload"]["cards"][0]["payload"][
        "name"
    ] = "Changed"
    from sagasmith_core.portable import portable_checksum

    tampered["checksum"] = portable_checksum(tampered)
    with pytest.raises(PortableContentError, match="checksum mismatch"):
        validate_addon_pack(tampered)

    public_without_license = copy.deepcopy(addon)
    public_without_license["metadata"].pop("license")
    public_without_license["checksum"] = portable_checksum(public_without_license)
    with pytest.raises(PortableContentError, match="license and attribution"):
        validate_addon_pack(public_without_license)

    wrong_policy = copy.deepcopy(addon)
    wrong_policy["payload"]["manifest"]["activation"]["preset_policy"] = "none"
    wrong_policy["checksum"] = portable_checksum(wrong_policy)
    with pytest.raises(PortableContentError, match="preset_policy must be library"):
        validate_addon_pack(wrong_policy)

    self_conflict = copy.deepcopy(addon)
    self_conflict["payload"]["manifest"]["conflicts"] = [addon["id"]]
    self_conflict["checksum"] = portable_checksum(self_conflict)
    with pytest.raises(PortableContentError, match="cannot conflict with itself"):
        validate_addon_pack(self_conflict)


def test_addon_readiness_is_strict_consistent_and_optional_for_legacy_packs() -> None:
    readiness = {
        "schema_version": 1,
        "source": {
            "item_count": 2,
            "verified_count": 2,
            "complete": True,
            "blockers": [],
        },
        "catalog": {
            "item_count": 2,
            "reviewed_count": 2,
            "complete": True,
            "blockers": [],
        },
        "selection": {
            "applicable_count": 1,
            "ready_count": 1,
            "not_applicable_count": 1,
            "complete": True,
            "blockers": [],
        },
        "runtime": {
            "item_count": 2,
            "resolved_count": 2,
            "modes": {"kernel_mechanic": 1, "agent_ruling": 1},
            "complete": True,
            "blockers": [],
        },
        "complete": True,
    }
    assert validate_addon_readiness(readiness) == readiness

    bad_total = copy.deepcopy(readiness)
    bad_total["complete"] = False
    with pytest.raises(PortableContentError, match="must equal all dimension"):
        validate_addon_readiness(bad_total)

    bad_selection = copy.deepcopy(readiness)
    bad_selection["selection"]["ready_count"] = 2
    with pytest.raises(PortableContentError, match="cannot exceed applicable_count"):
        validate_addon_readiness(bad_selection)

    bad_modes = copy.deepcopy(readiness)
    bad_modes["runtime"]["modes"]["agent_ruling"] = 2
    with pytest.raises(PortableContentError, match="sum to resolved_count"):
        validate_addon_readiness(bad_modes)

    unsupported = copy.deepcopy(readiness)
    unsupported["catalog"]["confidence"] = 1.0
    with pytest.raises(PortableContentError, match="unsupported fields"):
        validate_addon_readiness(unsupported)


def test_module_pack_round_trip_remaps_scenes_assets_reviews_and_actor_cards(
    database, tmp_path
) -> None:
    campaigns = CampaignService(database)
    modules = ModuleService(database)
    source_campaign = campaigns.create(system_id="dnd5e", name="Source module")
    target_campaign = campaigns.create(system_id="dnd5e", name="Target module")
    content = (
        "# Chapter One\nArrival.\n"
        "## Broken Gate\nThe gate is guarded by two wolves.\n"
        "## Inner Hall\nA sealed door leads below."
    )
    imported = modules.ingest(
        campaign_id=source_campaign.id,
        source_key="example.keep",
        title="The Keep",
        content=content,
    )
    scenes = modules.scene_index(source_campaign.id, module_id=imported.module_id)
    chunks = modules.list_chunks(
        source_campaign.id, imported.module_id, scene_id=scenes[0]["scene_id"]
    )
    modules.review_content(
        campaign_id=source_campaign.id,
        module_id=imported.module_id,
        scene_id=scenes[0]["scene_id"],
        content_key="gate.wolves",
        content_kind="encounter",
        normalized_content="Two wolves guard the gate.",
        source_chunk_ids=[chunks[0]["id"]],
        reviewer="test-dm",
        observation="Direct text evidence.",
    )
    asset_bytes = b"portable-map-bytes"
    source_asset = tmp_path / "keep-map.png"
    source_asset.write_bytes(asset_bytes)
    modules.register_asset(
        campaign_id=source_campaign.id,
        module_id=imported.module_id,
        source_path=str(source_asset),
        media_type="image/png",
        checksum=hashlib.sha256(asset_bytes).hexdigest(),
        metadata={"scene_key": scenes[0]["stable_key"], "asset_kind": "map"},
    )
    actor = build_actor_card(
        portable_id="example.keep.wolf",
        version="1.0.0",
        system_id="dnd5e",
        actor_type="monster",
        name="Gate Wolf",
        sheet={"schema_version": 2},
        notes={"schema_version": 2},
        bindings=[
            {
                "kind": "module_scene",
                "module_key": "example.keep",
                "scene_key": scenes[0]["stable_key"],
                "role": "hostile",
            }
        ],
    )

    package = modules.export_portable_pack(
        source_campaign.id,
        imported.module_id,
        portable_id="example.keep",
        actors=[actor],
        asset_loader=lambda path: Path(path).read_bytes(),
    )
    assert "two wolves" in package["payload"]["scene_atlas"][0]["content"].casefold()
    assert package["payload"]["scene_atlas"][0]["chunks"][0]["content"] == chunks[0][
        "content"
    ]

    def write_asset(module_id: str, asset: dict) -> str:
        target = tmp_path / "imported" / module_id / asset["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(asset["data_base64"]))
        return str(target)

    class ChangedLocalParser:
        def parse(self, _content):
            raise AssertionError("portable imports must not rerun local scene heuristics")

    result = modules.import_portable_pack(
        target_campaign.id,
        package,
        parser=ChangedLocalParser(),
        asset_writer=write_asset,
    )

    assert set(result["scene_map"]) == {scene["stable_key"] for scene in scenes}
    assert list(result["asset_map"]) == [package["payload"]["assets"][0]["asset_key"]]
    assert len(result["content_review_ids"]) == 1
    assert result["actor_cards"][0]["id"] == "example.keep.wolf"
    target_reviews = modules.list_content_reviews(target_campaign.id, result["module_id"])
    assert target_reviews[0]["content_key"] == "gate.wolves"
    assert target_reviews[0]["evidence"]["confidence"] == "reviewed_text"
    assert modules.list_assets(target_campaign.id, result["module_id"])[0][
        "metadata"
    ]["portable_asset_key"]


def test_module_pack_export_derives_chunk_from_real_scene_content(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Chunk fallback")
    modules = ModuleService(database)
    imported = modules.ingest(
        campaign_id=campaign.id,
        source_key="example.chunk-fallback",
        title="Chunk Fallback",
        content="# Chapter\nIntro.\n## Empty Index Row\nThe actual scene text remains available.",
    )
    scene = modules.scene_index(campaign.id, module_id=imported.module_id)[0]
    with database.transaction() as session:
        session.execute(delete(ModuleChunk).where(ModuleChunk.scene_id == scene["scene_id"]))

    package = modules.export_portable_pack(
        campaign.id,
        imported.module_id,
        portable_id="example.chunk-fallback",
    )

    exported = package["payload"]["scene_atlas"][0]
    assert exported["chunks"][0]["content"] == exported["content"]
    assert exported["chunks"][0]["metadata"] == {"derived_from_scene_content": True}


def test_module_pack_export_recovers_empty_legacy_scene_from_chunks(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Scene fallback")
    modules = ModuleService(database)
    imported = modules.ingest(
        campaign_id=campaign.id,
        source_key="example.scene-fallback",
        title="Scene Fallback",
        content="# Chapter\nIntro.\n## Indexed Text\nRecovered from the indexed chunk.",
    )
    scene = modules.scene_index(campaign.id, module_id=imported.module_id)[0]
    chunks = modules.list_chunks(campaign.id, imported.module_id, scene_id=scene["scene_id"])
    expected = "\n\n".join(chunk["content"] for chunk in chunks if chunk["content"].strip())
    with database.transaction() as session:
        row = session.get(ModuleScene, scene["scene_id"])
        assert row is not None
        row.content = ""

    package = modules.export_portable_pack(
        campaign.id,
        imported.module_id,
        portable_id="example.scene-fallback",
    )

    assert package["payload"]["scene_atlas"][0]["content"] == expected


def test_module_actor_binding_exports_local_cast_without_runtime_ids(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Cast")
    modules = ModuleService(database)
    imported = modules.ingest(
        campaign_id=campaign.id,
        source_key="example.cast",
        title="Cast Example",
        content="# Chapter\nIntro.\n## Audience\nThe magistrate waits.",
    )
    scene = modules.scene_index(campaign.id, module_id=imported.module_id)[0]
    actor = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        character_type="npc",
        name="Magistrate",
        sheet={"schema_version": 2},
        notes={"schema_version": 2},
    )
    binding = modules.bind_actor(
        campaign_id=campaign.id,
        module_id=imported.module_id,
        scene_id=scene["scene_id"],
        character_id=actor.id,
        portable_actor_id="example.cast.magistrate",
        binding_kind="cast",
        role="quest giver",
    )

    package = modules.export_portable_pack(
        campaign.id,
        imported.module_id,
        portable_id="example.cast",
    )
    card = package["payload"]["actors"][0]

    assert binding["scene_key"] == scene["stable_key"]
    assert card["id"] == "example.cast.magistrate"
    assert card["payload"]["bindings"][0]["scene_key"] == scene["stable_key"]
    assert card["payload"]["bindings"][0]["role"] == "quest giver"
    assert actor.id not in dumps_portable(package)
