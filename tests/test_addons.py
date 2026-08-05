from __future__ import annotations

import pytest
from sqlalchemy import delete

from sagasmith_core import (
    AddonError,
    AddonService,
    CampaignService,
    RulePackService,
    RuleProfileService,
    RuleService,
    SnapshotService,
    build_addon_pack,
    build_rule_pack,
)
from sagasmith_core.branches import BranchService
from sagasmith_core.models import CampaignAddonActivation
from sagasmith_core.rule_packs import RulePackError


def _rule_component(database, *, version: str = "1.0.0") -> dict:
    rules = RuleService(database)
    ingested = rules.ingest(
        system_id="dnd5e",
        source_key=f"example.addon-source-{version}",
        title="Example Addon Source",
        content="# Feature\nA source-backed example feature.",
        edition="2014",
        version=version,
        publication_id=f"example.addon-source-{version}",
        authority="supplement",
    )
    source = rules.export_portable_source(ingested.source_id)
    chunk = source["sections"][0]["chunks"][0]
    return build_rule_pack(
        portable_id="dnd5e.example-addon.rules",
        version=version,
        system_id="dnd5e",
        manifest={
            "id": "dnd5e.example-addon.rules",
            "version": version,
            "title": "Example Addon Rules",
            "namespace": "dnd5e.example-addon.rules",
            "system_id": "dnd5e",
            "editions": ["2014"],
            "dependencies": [],
            "conflicts": [],
            "capabilities": [],
        },
        artifacts=[
            {
                "id": "dnd5e.example-addon.rules.feature.test",
                "kind": "feature",
                "card": {"name": "Test Feature"},
                "source_citations": [
                    {
                        "source": f"rule-source:example.addon-source-{version}",
                        "source_key": f"example.addon-source-{version}",
                        "chunk_key": chunk["key"],
                        "source_checksum": source["checksum"],
                    }
                ],
            }
        ],
        mechanics=[],
        sources=[source],
        metadata={"distribution": "private", "license": "user-supplied"},
    )


def _addon(
    component: dict,
    *,
    addon_id: str = "dnd5e.example-addon",
    version: str = "1.0.0",
    conflicts: list[str] | None = None,
) -> dict:
    return build_addon_pack(
        portable_id=addon_id,
        version=version,
        system_id="dnd5e",
        manifest={
            "id": addon_id,
            "version": version,
            "system_id": "dnd5e",
            "title": "Example Addon",
            "editions": ["2014"],
            "classification": "third_party",
            "content_summary": {"feature": 1},
            "conflicts": list(conflicts or []),
            "activation": {
                "rule_policy": "branch",
                "preset_policy": "none",
                "module_policy": "none",
            },
        },
        components=[component],
        metadata={"distribution": "private", "license": "user-supplied"},
    )


def _install_rule_component(database, component: dict) -> None:
    payload = component["payload"]
    packs = RulePackService(database)
    draft = packs.save_draft(
        manifest=payload["manifest"],
        artifacts=payload["artifacts"],
        mechanics=payload["mechanics"],
        provenance={
            **dict(payload["provenance"]),
            "portable_package": {"checksum": component["checksum"]},
        },
    )
    assert draft.status == "validated"
    packs.install(component["id"], component["version"])


def _install_local_rule_component_without_portable_provenance(database, component: dict) -> None:
    payload = component["payload"]
    packs = RulePackService(database)
    draft = packs.save_draft(
        manifest=payload["manifest"],
        artifacts=payload["artifacts"],
        mechanics=payload["mechanics"],
        provenance=dict(payload["provenance"]),
    )
    assert draft.status == "validated"
    packs.install(component["id"], component["version"])


def test_addon_import_install_and_branch_activation_are_separate(database) -> None:
    component = _rule_component(database)
    addon_package = _addon(component)
    addons = AddonService(database)

    imported = addons.import_package(addon_package)
    assert imported.status == "imported"
    assert imported.validation_report["component_counts"] == {"rule_pack": 1}
    assert imported.validation_report["declared_content_summary"] == {"feature": 1}
    assert imported.validation_report["embedded_content_summary"] == {"feature": 1}
    assert addons.get_package(imported.addon_id, imported.version) == addon_package
    assert addons.component_status(imported.addon_id, imported.version)[0]["status"] == "missing"
    with pytest.raises(AddonError, match="not installed"):
        addons.install(imported.addon_id, imported.version)

    _install_rule_component(database, component)
    installed = addons.install(imported.addon_id, imported.version)
    assert installed.status == "installed"

    campaign = CampaignService(database).create(system_id="dnd5e", name="Addon")
    RuleProfileService(database).set(campaign.id, edition="2014")
    before = CampaignService(database).get(campaign.id).revision
    activation = addons.set_activation(
        campaign.id,
        addon_id=installed.addon_id,
        version=installed.version,
        expected_campaign_revision=before,
    )
    after = CampaignService(database).get(campaign.id).revision

    assert activation.enabled is True
    assert after == before + 1
    assert (
        RulePackService(database).effective_ruleset(campaign.id).lock[0]["pack_id"]
        == component["id"]
    )
    assert addons.activations(campaign.id) == [activation]

    disabled = addons.set_activation(
        campaign.id,
        addon_id=installed.addon_id,
        version=installed.version,
        enabled=False,
    )
    assert disabled.enabled is False
    assert RulePackService(database).effective_ruleset(campaign.id).lock == ()
    with pytest.raises(AddonError, match="activated"):
        addons.remove_version(installed.addon_id, installed.version)


def test_addon_accepts_exact_plugin_proven_local_component_equivalence(database) -> None:
    component = _rule_component(database)
    addon_package = _addon(component)
    addons = AddonService(database)
    imported = addons.import_package(addon_package)
    _install_local_rule_component_without_portable_provenance(database, component)

    with pytest.raises(AddonError, match="unverified"):
        addons.install(imported.addon_id, imported.version)
    proof = addons.record_component_equivalence(
        imported.addon_id,
        imported.version,
        kind="rule_pack",
        component_id=component["id"],
        component_version=component["version"],
        checksum=component["checksum"],
        basis="portable_definition_checksum",
        proof_checksum=component["metadata"]["definition_checksum"],
    )

    assert proof["checksum"] == component["checksum"]
    assert (
        addons.component_status(imported.addon_id, imported.version)[0]["checksum_status"]
        == "match"
    )
    assert addons.install(imported.addon_id, imported.version).status == "installed"


def test_addon_activation_rejects_wrong_edition_and_active_combat(database) -> None:
    component = _rule_component(database)
    addon_package = _addon(component)
    _install_rule_component(database, component)
    addons = AddonService(database)
    addons.import_package(addon_package)
    addons.install(addon_package["id"], addon_package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Wrong edition")
    RuleProfileService(database).set(campaign.id, edition="2024")

    with pytest.raises(AddonError, match="does not support"):
        addons.set_activation(
            campaign.id,
            addon_id=addon_package["id"],
            version=addon_package["version"],
        )

    RuleProfileService(database).set(campaign.id, edition="2014")
    CampaignService(database).update(campaign.id, state={"combat": {"active": True}})
    with pytest.raises(AddonError, match="active combat"):
        addons.set_activation(
            campaign.id,
            addon_id=addon_package["id"],
            version=addon_package["version"],
        )


def test_addon_lock_is_preserved_by_snapshots_and_branch_forks(database) -> None:
    component = _rule_component(database)
    addon_package = _addon(component)
    _install_rule_component(database, component)
    addons = AddonService(database)
    addons.import_package(addon_package)
    addons.install(addon_package["id"], addon_package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Snapshot addon")
    RuleProfileService(database).set(campaign.id, edition="2014")
    activation = addons.set_activation(
        campaign.id,
        addon_id=addon_package["id"],
        version=addon_package["version"],
    )

    snapshot = SnapshotService(database).create(campaign.id, label="addon enabled")
    fork = BranchService(database).create(
        campaign.id,
        name="addon fork",
        from_snapshot_id=snapshot.id,
        checkout=True,
    )

    assert addons.activations(campaign.id, branch_id=fork.id)[0].checksum == activation.checksum
    assert RulePackService(database).effective_ruleset(campaign.id, branch_id=fork.id).lock
    original_branch_id = next(
        item.id for item in BranchService(database).list(campaign.id) if item.id != fork.id
    )
    comparison = BranchService(database).compare(
        campaign.id,
        original_branch_id,
        fork.id,
    )
    assert comparison["addon_lock"] == {
        "left_only": [],
        "right_only": [],
        "changed": [],
    }
    addons.set_activation(
        campaign.id,
        addon_id=addon_package["id"],
        version=addon_package["version"],
        branch_id=fork.id,
        options={"rule_options": {component["id"]: {"branch_choice": "fork"}}},
    )
    changed = BranchService(database).compare(
        campaign.id,
        original_branch_id,
        fork.id,
    )
    assert changed["addon_lock"]["changed"] == [addon_package["id"]]
    assert changed["rule_lock"]["changed"] == [component["id"]]
    with database.transaction() as session:
        session.execute(
            delete(CampaignAddonActivation).where(
                CampaignAddonActivation.addon_id == addon_package["id"]
            )
        )
    with pytest.raises(AddonError, match="referenced by a snapshot"):
        addons.remove_version(addon_package["id"], addon_package["version"])


def test_shared_rule_component_tracks_options_per_addon_owner(database) -> None:
    component = _rule_component(database)
    _install_rule_component(database, component)
    addons = AddonService(database)
    first = _addon(component, addon_id="dnd5e.example-addon.first")
    second = _addon(component, addon_id="dnd5e.example-addon.second")
    for package in (first, second):
        addons.import_package(package)
        addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Shared addon")
    RuleProfileService(database).set(campaign.id, edition="2014")

    addons.set_activation(
        campaign.id,
        addon_id=first["id"],
        version=first["version"],
        options={"rule_options": {component["id"]: {"first_only": True, "shared": "same"}}},
    )
    addons.set_activation(
        campaign.id,
        addon_id=second["id"],
        version=second["version"],
        options={"rule_options": {component["id"]: {"second_only": True, "shared": "same"}}},
    )
    lock = RulePackService(database).effective_ruleset(campaign.id).lock[0]
    assert lock["options"]["first_only"] is True
    assert lock["options"]["second_only"] is True
    assert lock["options"]["shared"] == "same"
    assert lock["options"]["_addon_ids"] == sorted([first["id"], second["id"]])

    addons.set_activation(
        campaign.id,
        addon_id=first["id"],
        version=first["version"],
        enabled=False,
    )
    lock = RulePackService(database).effective_ruleset(campaign.id).lock[0]
    assert "first_only" not in lock["options"]
    assert lock["options"]["second_only"] is True
    assert lock["options"]["shared"] == "same"
    assert lock["options"]["_addon_ids"] == [second["id"]]

    with pytest.raises(RulePackError, match="owned by active addons"):
        RulePackService(database).set_activation(
            campaign.id,
            pack_id=component["id"],
            version=component["version"],
            enabled=False,
        )
    with pytest.raises(RulePackError, match="owned by active addons"):
        RulePackService(database).remove_activation(campaign.id, component["id"])


def test_shared_rule_component_rejects_conflicting_owner_options_atomically(database) -> None:
    component = _rule_component(database)
    _install_rule_component(database, component)
    addons = AddonService(database)
    first = _addon(component, addon_id="dnd5e.example-addon.first")
    second = _addon(component, addon_id="dnd5e.example-addon.second")
    for package in (first, second):
        addons.import_package(package)
        addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Conflict")
    RuleProfileService(database).set(campaign.id, edition="2014")
    addons.set_activation(
        campaign.id,
        addon_id=first["id"],
        version=first["version"],
        options={"rule_options": {component["id"]: {"mode": "first"}}},
    )
    revision = CampaignService(database).get(campaign.id).revision

    with pytest.raises(AddonError, match="rule options conflict"):
        addons.set_activation(
            campaign.id,
            addon_id=second["id"],
            version=second["version"],
            options={"rule_options": {component["id"]: {"mode": "second"}}},
        )

    assert CampaignService(database).get(campaign.id).revision == revision
    assert [item.addon_id for item in addons.activations(campaign.id) if item.enabled] == [
        first["id"]
    ]
    lock = RulePackService(database).effective_ruleset(campaign.id).lock[0]
    assert lock["options"]["mode"] == "first"


def test_addon_disable_restores_preexisting_manual_rule_activation(database) -> None:
    component = _rule_component(database)
    _install_rule_component(database, component)
    addons = AddonService(database)
    package = _addon(component)
    addons.import_package(package)
    addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Manual owner")
    RuleProfileService(database).set(campaign.id, edition="2014")
    RulePackService(database).set_activation(
        campaign.id,
        pack_id=component["id"],
        version=component["version"],
        options={"manual_choice": "preserved"},
    )

    addons.set_activation(
        campaign.id,
        addon_id=package["id"],
        version=package["version"],
        options={"rule_options": {component["id"]: {"addon_choice": True}}},
    )
    addons.set_activation(
        campaign.id,
        addon_id=package["id"],
        version=package["version"],
        enabled=False,
    )

    lock = RulePackService(database).effective_ruleset(campaign.id).lock[0]
    assert lock["options"]["manual_choice"] == "preserved"
    assert "addon_choice" not in lock["options"]
    assert lock["options"]["_addon_ids"] == []
    assert lock["options"]["_manual_activation_preserved"] is True


@pytest.mark.parametrize("activate_conflicting_first", [False, True])
def test_addon_conflicts_are_enforced_in_both_directions(
    database, activate_conflicting_first: bool
) -> None:
    component = _rule_component(database)
    _install_rule_component(database, component)
    first_id = "dnd5e.example-addon.first"
    second_id = "dnd5e.example-addon.second"
    first = _addon(component, addon_id=first_id, conflicts=[second_id])
    second = _addon(component, addon_id=second_id)
    addons = AddonService(database)
    for package in (first, second):
        addons.import_package(package)
        addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Conflicts")
    RuleProfileService(database).set(campaign.id, edition="2014")
    active, blocked = (first, second) if activate_conflicting_first else (second, first)
    addons.set_activation(
        campaign.id,
        addon_id=active["id"],
        version=active["version"],
    )

    with pytest.raises(AddonError, match="conflicts with"):
        addons.set_activation(
            campaign.id,
            addon_id=blocked["id"],
            version=blocked["version"],
        )


def test_addon_version_switch_releases_the_previous_exact_component_lock(database) -> None:
    component_v1 = _rule_component(database, version="1.0.0")
    component_v2 = _rule_component(database, version="2.0.0")
    for component in (component_v1, component_v2):
        _install_rule_component(database, component)
    addon_id = "dnd5e.example-addon.versioned"
    addon_v1 = _addon(component_v1, addon_id=addon_id, version="1.0.0")
    addon_v2 = _addon(component_v2, addon_id=addon_id, version="2.0.0")
    addons = AddonService(database)
    for package in (addon_v1, addon_v2):
        addons.import_package(package)
        addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Upgrade")
    RuleProfileService(database).set(campaign.id, edition="2014")
    addons.set_activation(
        campaign.id,
        addon_id=addon_id,
        version="1.0.0",
        options={"rule_options": {component_v1["id"]: {"generation": 1}}},
    )

    switched = addons.set_activation(
        campaign.id,
        addon_id=addon_id,
        version="2.0.0",
        options={"rule_options": {component_v2["id"]: {"generation": 2}}},
    )

    assert switched.version == "2.0.0"
    lock = RulePackService(database).effective_ruleset(campaign.id).lock[0]
    assert lock["version"] == "2.0.0"
    assert lock["options"]["generation"] == 2
    with pytest.raises(AddonError, match="must match the active exact version"):
        addons.set_activation(
            campaign.id,
            addon_id=addon_id,
            version="1.0.0",
            enabled=False,
        )
    disabled = addons.set_activation(
        campaign.id,
        addon_id=addon_id,
        version="2.0.0",
        enabled=False,
    )
    assert disabled.enabled is False
    assert RulePackService(database).effective_ruleset(campaign.id).lock == ()
