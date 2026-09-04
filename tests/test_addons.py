from __future__ import annotations

import hashlib

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
    build_content_package,
    build_source_bundle,
)
from sagasmith_core.branches import BranchService
from sagasmith_core.models import CampaignAddonActivation
from sagasmith_core.rule_packs import RulePackError


def _rule_component(
    database,
    *,
    pack_id: str = "dnd5e.example-addon.rules",
    version: str = "1.0.0",
    system_id: str = "dnd5e",
    editions: list[str] | None = None,
    dependencies: list[str | dict] | None = None,
) -> dict:
    rules = RuleService(database)
    ingested = rules.ingest(
        system_id=system_id,
        source_key=f"{pack_id}.source-{version}",
        title="Example Addon Source",
        content="# Feature\nA source-backed example feature.",
        edition="2014",
        version=version,
        publication_id=f"{pack_id}.source-{version}",
        authority="supplement",
    )
    source = rules.export_indexed_source(ingested.source_id)
    chunk = source["sections"][0]["chunks"][0]
    manifest = {
            "id": pack_id,
            "version": version,
            "title": "Example Addon Rules",
            "namespace": pack_id,
            "system_id": system_id,
            "editions": list(editions or ["2014"]),
            "dependencies": list(dependencies or []),
            "conflicts": [],
            "capabilities": [],
        }
    artifacts = [
            {
                "id": f"{pack_id}.feature.test",
                "kind": "feature",
                "card": {"name": "Test Feature"},
                "source_citations": [
                    {
                        "source": f"rule-source:{pack_id}.source-{version}",
                        "source_key": f"{pack_id}.source-{version}",
                        "chunk_key": chunk["key"],
                        "source_checksum": source["checksum"],
                    }
                ],
            }
        ]
    definition_checksum = hashlib.sha256(
        f"{pack_id}:{version}".encode()
    ).hexdigest()
    return {
        "id": pack_id,
        "version": version,
        "system_id": system_id,
        "payload": {
            "manifest": manifest,
            "artifacts": artifacts,
            "mechanics": [],
            "provenance": {},
            "sources": [source],
        },
        "metadata": {
            "distribution": "private",
            "license": "user-supplied",
            "definition_checksum": definition_checksum,
        },
    }


def _addon(
    component: dict,
    *,
    addon_id: str = "dnd5e.example-addon",
    version: str = "1.0.0",
    conflicts: list[str] | None = None,
) -> dict:
    raw_source = component["payload"]["sources"][0]
    section = raw_source["sections"][0]
    text = section["content"]
    source, asset, _blob = build_source_bundle(
        source_key=raw_source["source_key"],
        title=raw_source["title"],
        normalized_text=text,
        edition=raw_source["edition"],
        sections=[
            {
                "ordinal": 0,
                "parent_ordinal": None,
                "level": 1,
                "title": section["title"],
                "path": section["path"],
                "start_offset": 0,
                "end_offset": len(text),
                "chunks": [
                    {
                        "key": section["chunks"][0]["key"],
                        "ordinal": 0,
                        "heading_path": section["chunks"][0]["heading_path"],
                        "start_offset": 0,
                        "end_offset": len(text),
                        "token_count": len(text.split()),
                        "page_start": None,
                        "page_end": None,
                        "metadata": {},
                    }
                ],
            }
        ],
        license="user-supplied",
        attribution="test",
    )
    manifest = {
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
    }
    return build_content_package(
        kind="addon",
        package_id=addon_id,
        version=version,
        system_id="dnd5e",
        manifest=manifest,
        sources=[source],
        assets=[asset],
        content_reviews=[],
        actors=[],
        content={
            "classification": "third_party",
            "editions": ["2014"],
            "activation": manifest["activation"],
            "conflicts": manifest["conflicts"],
            "rule_definitions": [
                {
                    "id": component["id"],
                    "version": component["version"],
                    "definition_checksum": component["metadata"]["definition_checksum"],
                    "manifest": component["payload"]["manifest"],
                }
            ],
            "artifacts": [
                {
                    **component["payload"]["artifacts"][0],
                    "rule_definition_id": component["id"],
                }
            ],
            "mechanics": [],
        },
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
            "content_definition": {
                "definition_checksum": component["metadata"]["definition_checksum"]
            },
        },
    )
    assert draft.status == "validated"
    packs.install(component["id"], component["version"])


def _install_local_rule_component_without_package_provenance(database, component: dict) -> None:
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


def test_addon_activation_owns_only_its_exact_recursive_rule_dependency_closure(
    database,
) -> None:
    packs = RulePackService(database)
    leaf = _rule_component(database, pack_id="dnd5e.dependency.leaf")
    _install_rule_component(database, leaf)
    middle = _rule_component(
        database,
        pack_id="dnd5e.dependency.middle",
        dependencies=[
            {
                "id": leaf["id"],
                "version": leaf["version"],
                "checksum": leaf["metadata"]["definition_checksum"],
            }
        ],
    )
    _install_rule_component(database, middle)
    middle_runtime = packs.get_version(middle["id"], middle["version"])
    root = _rule_component(
        database,
        dependencies=[
            {
                "id": middle["id"],
                "version": middle["version"],
                "checksum": middle_runtime.checksum,
            }
        ],
    )
    unrelated = _rule_component(database, pack_id="dnd5e.dependency.unrelated")
    for component in (root, unrelated):
        _install_rule_component(database, component)
    package = _addon(root)
    addons = AddonService(database)
    addons.import_package(package)
    addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Closure")
    RuleProfileService(database).set(campaign.id, edition="2014")

    activated = addons.set_activation(
        campaign.id,
        addon_id=package["id"],
        version=package["version"],
    )

    assert activated.enabled is True
    lock = packs.effective_ruleset(campaign.id).lock
    assert {item["pack_id"] for item in lock} == {
        leaf["id"],
        middle["id"],
        root["id"],
    }
    assert unrelated["id"] not in {item["pack_id"] for item in lock}
    assert {
        item["pack_id"]: item["options"]["_addon_ids"] for item in lock
    } == {
        leaf["id"]: [package["id"]],
        middle["id"]: [package["id"]],
        root["id"]: [package["id"]],
    }

    addons.set_activation(
        campaign.id,
        addon_id=package["id"],
        version=package["version"],
        enabled=False,
    )
    assert packs.effective_ruleset(campaign.id).lock == ()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "not installed"),
        ("inactive", "not installed"),
        ("wrong-version", "not installed"),
        ("stale-checksum", "requires checksum"),
        ("unpinned", "must pin exact version and checksum"),
    ],
)
def test_addon_dependency_activation_rejects_non_exact_or_inactive_closure_atomically(
    database,
    case: str,
    message: str,
) -> None:
    dependency_id = "dnd5e.dependency.required"
    dependency = _rule_component(database, pack_id=dependency_id)
    if case in {"inactive", "wrong-version", "stale-checksum"}:
        if case == "inactive":
            payload = dependency["payload"]
            draft = RulePackService(database).save_draft(
                manifest=payload["manifest"],
                artifacts=payload["artifacts"],
                mechanics=payload["mechanics"],
                provenance={
                    "content_definition": {
                        "definition_checksum": dependency["metadata"][
                            "definition_checksum"
                        ]
                    }
                },
            )
            assert draft.status == "validated"
        else:
            _install_rule_component(database, dependency)
    dependency_spec: str | dict = dependency_id
    if case != "unpinned":
        dependency_spec = {
            "id": dependency_id,
            "version": "2.0.0" if case == "wrong-version" else "1.0.0",
            "checksum": (
                "f" * 64
                if case == "stale-checksum"
                else dependency["metadata"]["definition_checksum"]
            ),
        }
    root = _rule_component(database, dependencies=[dependency_spec])
    _install_rule_component(database, root)
    package = _addon(root)
    addons = AddonService(database)
    addons.import_package(package)
    addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name=case)
    RuleProfileService(database).set(campaign.id, edition="2014")
    before_revision = CampaignService(database).get(campaign.id).revision

    with pytest.raises(AddonError, match=message):
        addons.set_activation(
            campaign.id,
            addon_id=package["id"],
            version=package["version"],
        )

    assert CampaignService(database).get(campaign.id).revision == before_revision
    assert addons.activations(campaign.id) == []
    assert RulePackService(database).effective_ruleset(campaign.id).lock == ()


@pytest.mark.parametrize(
    ("dependency_system", "dependency_editions", "message"),
    [
        ("coc7e", ["2014"], "incompatible with campaign system"),
        ("dnd5e", ["2024"], "does not support campaign edition 2014"),
    ],
)
def test_addon_dependency_activation_rejects_incompatible_closure_atomically(
    database,
    dependency_system: str,
    dependency_editions: list[str],
    message: str,
) -> None:
    dependency = _rule_component(
        database,
        pack_id=f"{dependency_system}.dependency.required",
        system_id=dependency_system,
        editions=dependency_editions,
    )
    _install_rule_component(database, dependency)
    root = _rule_component(
        database,
        dependencies=[
            {
                "id": dependency["id"],
                "version": dependency["version"],
                "checksum": dependency["metadata"]["definition_checksum"],
            }
        ],
    )
    _install_rule_component(database, root)
    package = _addon(root)
    addons = AddonService(database)
    addons.import_package(package)
    addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Incompatible")
    RuleProfileService(database).set(campaign.id, edition="2014")
    before_revision = CampaignService(database).get(campaign.id).revision

    with pytest.raises(AddonError, match=message):
        addons.set_activation(
            campaign.id,
            addon_id=package["id"],
            version=package["version"],
        )

    assert CampaignService(database).get(campaign.id).revision == before_revision
    assert addons.activations(campaign.id) == []
    assert RulePackService(database).effective_ruleset(campaign.id).lock == ()


def test_addon_dependency_activation_rejects_cycles_atomically(database) -> None:
    first_id = "dnd5e.dependency.cycle.first"
    second_id = "dnd5e.dependency.cycle.second"
    first_checksum = hashlib.sha256(f"{first_id}:1.0.0".encode()).hexdigest()
    second_checksum = hashlib.sha256(f"{second_id}:1.0.0".encode()).hexdigest()
    first = _rule_component(
        database,
        pack_id=first_id,
        dependencies=[
            {"id": second_id, "version": "1.0.0", "checksum": second_checksum}
        ],
    )
    second = _rule_component(
        database,
        pack_id=second_id,
        dependencies=[
            {"id": first_id, "version": "1.0.0", "checksum": first_checksum}
        ],
    )
    for component in (first, second):
        _install_rule_component(database, component)
    package = _addon(first)
    addons = AddonService(database)
    addons.import_package(package)
    addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Cycle")
    RuleProfileService(database).set(campaign.id, edition="2014")
    before_revision = CampaignService(database).get(campaign.id).revision

    with pytest.raises(AddonError, match="dependency cycle"):
        addons.set_activation(
            campaign.id,
            addon_id=package["id"],
            version=package["version"],
        )

    assert CampaignService(database).get(campaign.id).revision == before_revision
    assert addons.activations(campaign.id) == []
    assert RulePackService(database).effective_ruleset(campaign.id).lock == ()


def test_addon_dependency_activation_rejects_ambiguous_exact_versions(database) -> None:
    shared_id = "dnd5e.dependency.shared"
    shared_v1 = _rule_component(database, pack_id=shared_id, version="1.0.0")
    shared_v2 = _rule_component(database, pack_id=shared_id, version="2.0.0")
    for component in (shared_v1, shared_v2):
        _install_rule_component(database, component)
    branches = []
    for name, dependency in (("left", shared_v1), ("right", shared_v2)):
        branch = _rule_component(
            database,
            pack_id=f"dnd5e.dependency.{name}",
            dependencies=[
                {
                    "id": dependency["id"],
                    "version": dependency["version"],
                    "checksum": dependency["metadata"]["definition_checksum"],
                }
            ],
        )
        _install_rule_component(database, branch)
        branches.append(branch)
    root = _rule_component(
        database,
        dependencies=[
            {
                "id": branch["id"],
                "version": branch["version"],
                "checksum": branch["metadata"]["definition_checksum"],
            }
            for branch in branches
        ],
    )
    _install_rule_component(database, root)
    package = _addon(root)
    addons = AddonService(database)
    addons.import_package(package)
    addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Ambiguous")
    RuleProfileService(database).set(campaign.id, edition="2014")
    before_revision = CampaignService(database).get(campaign.id).revision

    with pytest.raises(AddonError, match="requirements are ambiguous"):
        addons.set_activation(
            campaign.id,
            addon_id=package["id"],
            version=package["version"],
        )

    assert CampaignService(database).get(campaign.id).revision == before_revision
    assert addons.activations(campaign.id) == []
    assert RulePackService(database).effective_ruleset(campaign.id).lock == ()


def test_shared_addon_dependency_remains_owned_until_the_last_addon_is_disabled(
    database,
) -> None:
    shared = _rule_component(database, pack_id="dnd5e.dependency.shared")
    _install_rule_component(database, shared)
    addons = AddonService(database)
    packages = []
    for name in ("first", "second"):
        root = _rule_component(
            database,
            pack_id=f"dnd5e.dependency.root.{name}",
            dependencies=[
                {
                    "id": shared["id"],
                    "version": shared["version"],
                    "checksum": shared["metadata"]["definition_checksum"],
                }
            ],
        )
        _install_rule_component(database, root)
        package = _addon(root, addon_id=f"dnd5e.example-addon.{name}")
        addons.import_package(package)
        addons.install(package["id"], package["version"])
        packages.append(package)
    campaign = CampaignService(database).create(system_id="dnd5e", name="Shared closure")
    RuleProfileService(database).set(campaign.id, edition="2014")
    for package in packages:
        addons.set_activation(
            campaign.id,
            addon_id=package["id"],
            version=package["version"],
        )

    addons.set_activation(
        campaign.id,
        addon_id=packages[0]["id"],
        version=packages[0]["version"],
        enabled=False,
    )

    lock = RulePackService(database).effective_ruleset(campaign.id).lock
    assert {item["pack_id"] for item in lock} == {
        "dnd5e.dependency.root.second",
        shared["id"],
    }
    shared_lock = next(item for item in lock if item["pack_id"] == shared["id"])
    assert shared_lock["options"]["_addon_ids"] == [packages[1]["id"]]

    addons.set_activation(
        campaign.id,
        addon_id=packages[1]["id"],
        version=packages[1]["version"],
        enabled=False,
    )
    assert RulePackService(database).effective_ruleset(campaign.id).lock == ()


def test_addon_dependency_disable_preserves_preexisting_manual_activation(database) -> None:
    dependency = _rule_component(database, pack_id="dnd5e.dependency.manual")
    _install_rule_component(database, dependency)
    root = _rule_component(
        database,
        dependencies=[
            {
                "id": dependency["id"],
                "version": dependency["version"],
                "checksum": dependency["metadata"]["definition_checksum"],
            }
        ],
    )
    _install_rule_component(database, root)
    package = _addon(root)
    addons = AddonService(database)
    addons.import_package(package)
    addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Manual closure")
    RuleProfileService(database).set(campaign.id, edition="2014")
    packs = RulePackService(database)
    packs.set_activation(
        campaign.id,
        pack_id=dependency["id"],
        version=dependency["version"],
        options={"manual": "preserved"},
    )

    addons.set_activation(
        campaign.id,
        addon_id=package["id"],
        version=package["version"],
    )
    addons.set_activation(
        campaign.id,
        addon_id=package["id"],
        version=package["version"],
        enabled=False,
    )

    lock = packs.effective_ruleset(campaign.id).lock
    assert [item["pack_id"] for item in lock] == [dependency["id"]]
    assert lock[0]["options"]["manual"] == "preserved"
    assert lock[0]["options"]["_addon_ids"] == []
    assert lock[0]["options"]["_manual_activation_preserved"] is True


def test_addon_version_switch_replaces_the_exact_dependency_closure(database) -> None:
    dependency_id = "dnd5e.dependency.versioned"
    root_id = "dnd5e.example-addon.rules"
    addon_id = "dnd5e.example-addon.versioned-closure"
    addons = AddonService(database)
    packages = []
    for version in ("1.0.0", "2.0.0"):
        dependency = _rule_component(
            database,
            pack_id=dependency_id,
            version=version,
        )
        _install_rule_component(database, dependency)
        root = _rule_component(
            database,
            pack_id=root_id,
            version=version,
            dependencies=[
                {
                    "id": dependency["id"],
                    "version": dependency["version"],
                    "checksum": dependency["metadata"]["definition_checksum"],
                }
            ],
        )
        _install_rule_component(database, root)
        package = _addon(root, addon_id=addon_id, version=version)
        addons.import_package(package)
        addons.install(package["id"], package["version"])
        packages.append(package)
    campaign = CampaignService(database).create(system_id="dnd5e", name="Closure upgrade")
    RuleProfileService(database).set(campaign.id, edition="2014")
    addons.set_activation(campaign.id, addon_id=addon_id, version="1.0.0")

    upgraded = addons.set_activation(campaign.id, addon_id=addon_id, version="2.0.0")

    assert upgraded.version == "2.0.0"
    lock = RulePackService(database).effective_ruleset(campaign.id).lock
    assert {item["pack_id"]: item["version"] for item in lock} == {
        dependency_id: "2.0.0",
        root_id: "2.0.0",
    }
    assert all(item["options"]["_addon_ids"] == [addon_id] for item in lock)


def test_addon_dependency_activation_rejects_overdeep_closure_without_recursion(
    database,
) -> None:
    next_component: dict | None = None
    for index in reversed(range(130)):
        dependencies = []
        if next_component is not None:
            dependencies.append(
                {
                    "id": next_component["id"],
                    "version": next_component["version"],
                    "checksum": next_component["metadata"]["definition_checksum"],
                }
            )
        component = _rule_component(
            database,
            pack_id=f"dnd5e.dependency.deep-{index}",
            dependencies=dependencies,
        )
        _install_rule_component(database, component)
        next_component = component
    assert next_component is not None
    package = _addon(next_component)
    addons = AddonService(database)
    addons.import_package(package)
    addons.install(package["id"], package["version"])
    campaign = CampaignService(database).create(system_id="dnd5e", name="Deep closure")
    RuleProfileService(database).set(campaign.id, edition="2014")
    before_revision = CampaignService(database).get(campaign.id).revision

    with pytest.raises(AddonError, match="safe depth limit"):
        addons.set_activation(
            campaign.id,
            addon_id=package["id"],
            version=package["version"],
        )

    assert CampaignService(database).get(campaign.id).revision == before_revision
    assert addons.activations(campaign.id) == []
    assert RulePackService(database).effective_ruleset(campaign.id).lock == ()


def test_addon_accepts_exact_plugin_proven_local_component_equivalence(database) -> None:
    component = _rule_component(database)
    addon_package = _addon(component)
    addons = AddonService(database)
    imported = addons.import_package(addon_package)
    _install_local_rule_component_without_package_provenance(database, component)

    with pytest.raises(AddonError, match="unverified"):
        addons.install(imported.addon_id, imported.version)
    proof = addons.record_component_equivalence(
        imported.addon_id,
        imported.version,
        kind="rule_pack",
        component_id=component["id"],
        component_version=component["version"],
        checksum=component["metadata"]["definition_checksum"],
        basis="content_definition_checksum",
        proof_checksum=component["metadata"]["definition_checksum"],
    )

    assert proof["checksum"] == component["metadata"]["definition_checksum"]
    assert (
        addons.component_status(imported.addon_id, imported.version)[0]["checksum_status"]
        == "match"
    )
    assert addons.install(imported.addon_id, imported.version).status == "installed"


def test_addon_activation_rejects_wrong_edition_and_runtime_lock(database) -> None:
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
    CampaignService(database).update(
        campaign.id,
        state={
            "mutation_locks": [
                {"domains": ["addon_activation"], "reason": "active encounter"}
            ]
        },
    )
    with pytest.raises(AddonError, match="active encounter"):
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
