import base64
import copy
import hashlib
from pathlib import Path

import pytest

from sagasmith_core.campaigns import CampaignService
from sagasmith_core.characters import CharacterService
from sagasmith_core.modules import ModuleService
from sagasmith_core.portable import (
    PortableContentError,
    build_actor_card,
    dumps_portable,
    loads_portable,
    validate_actor_card,
)


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

    card = characters.export_portable_card(
        source.id,
        portable_id="example.mira",
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
