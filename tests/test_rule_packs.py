import pytest

from sagasmith_core import (
    CampaignService,
    CharacterService,
    IdempotencyService,
    IdempotencyWrite,
    RulePackService,
    RuleProfileService,
    SnapshotService,
)
from sagasmith_core.branches import BranchService
from sagasmith_core.rule_packs import RulePackError, RulesetUnavailableError


def _pack(pack_id: str = "dnd5e.xgte", *, dependencies=None, conflicts=None):
    return {
        "id": pack_id,
        "version": "1.0.0",
        "title": "Optional rules",
        "namespace": pack_id,
        "system_id": "dnd5e",
        "editions": ["2014"],
        "dependencies": dependencies or [],
        "conflicts": conflicts or [],
        "capabilities": ["activity.after"],
    }


def test_rule_pack_install_activation_and_branch_lock(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Rules")
    RuleProfileService(database).set(campaign.id, edition="2014")
    packs = RulePackService(database)
    draft = packs.save_draft(
        manifest=_pack(),
        artifacts=[
            {
                "id": "dnd5e.xgte.feature.test",
                "kind": "feature",
                "card": {"name": "Test feature"},
            }
        ],
        mechanics=[
            {
                "id": "dnd5e.xgte.mechanic.test",
                "event": "activity.after",
                "operations": [{"op": "resource.recover", "path": "resources.test", "amount": 1}],
                "citations": [{"source": "local:xgte", "section": "test"}],
            }
        ],
    )
    assert draft.status == "validated"
    packs.install("dnd5e.xgte", "1.0.0")
    activation = packs.set_activation(campaign.id, pack_id="dnd5e.xgte", version="1.0.0")
    effective = packs.effective_ruleset(campaign.id)
    assert effective.lock[0]["checksum"] == activation.checksum
    assert effective.mechanics[0]["id"] == "dnd5e.xgte.mechanic.test"

    snapshot = SnapshotService(database).create(campaign.id, label="with rules")
    fork = BranchService(database).create(
        campaign.id, name="without rules", from_snapshot_id=snapshot.id, checkout=True
    )
    assert (
        packs.effective_ruleset(campaign.id, branch_id=fork.id).fingerprint == effective.fingerprint
    )
    packs.remove_activation(campaign.id, "dnd5e.xgte", branch_id=fork.id)
    assert packs.effective_ruleset(campaign.id, branch_id=fork.id).lock == ()
    source_branch = next(
        item for item in BranchService(database).list(campaign.id) if item.id != fork.id
    )
    assert packs.effective_ruleset(campaign.id, branch_id=source_branch.id).lock
    comparison = BranchService(database).compare(campaign.id, source_branch.id, fork.id)
    assert comparison["rule_lock"]["left_only"] == ["dnd5e.xgte"]
    packs.remove_activation(campaign.id, "dnd5e.xgte", branch_id=source_branch.id)
    with pytest.raises(RulePackError, match="snapshot"):
        packs.remove_version("dnd5e.xgte", "1.0.0")


def test_rule_pack_keeps_advice_without_blocking_validation(database) -> None:
    draft = RulePackService(database).save_draft(
        manifest=_pack(),
        additional_warnings=["declarative tests do not cover optional behavior"],
    )

    assert draft.status == "validated"
    assert draft.validation_report == {
        "valid": True,
        "errors": [],
        "warnings": ["declarative tests do not cover optional behavior"],
    }


def test_rule_profile_cannot_diverge_while_runtime_locked(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Locked combat rules")
    campaigns.update(
        campaign.id,
        state={
            "mutation_locks": [
                {"domains": ["rule_profile"], "reason": "active encounter"}
            ]
        },
    )

    with pytest.raises(ValueError, match="rule profile cannot change while locked"):
        RuleProfileService(database).set(campaign.id, edition="2024")


def test_rule_profile_allows_only_declared_option_maintenance_while_locked(
    database,
) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Core maintenance")
    profiles = RuleProfileService(database)
    profiles.set(
        campaign.id,
        edition="2014",
        locale="en",
        options={
            "house_option": "preserved",
            "_core_rule_pack_lock": {"fingerprint": "old"},
        },
    )
    campaign = campaigns.get(campaign.id)
    campaigns.update(
        campaign.id,
        state={
            "mutation_locks": [
                {
                    "domains": ["rule_profile"],
                    "reason": "active encounter",
                    "mutable_option_keys": ["_core_rule_pack_lock"],
                }
            ]
        },
        expected_revision=campaign.revision,
    )

    maintained = profiles.set(
        campaign.id,
        edition="2014",
        locale="en",
        options={
            "house_option": "preserved",
            "_core_rule_pack_lock": {"fingerprint": "new"},
        },
    )

    assert maintained.options["house_option"] == "preserved"
    assert maintained.options["_core_rule_pack_lock"]["fingerprint"] == "new"
    with pytest.raises(ValueError, match="outside its explicit allowlist"):
        profiles.set(
            campaign.id,
            edition="2014",
            locale="en",
            options={
                "house_option": "changed",
                "_core_rule_pack_lock": {"fingerprint": "newer"},
            },
        )
    with pytest.raises(ValueError, match="cannot change edition, locale, or publications"):
        profiles.set(
            campaign.id,
            edition="2014",
            locale="zh-CN",
            options=maintained.options,
        )


def test_rule_profile_cannot_diverge_from_existing_character_editions(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Locked actor rules")
    profiles = RuleProfileService(database)
    profiles.set(campaign.id, edition="2014")
    CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Existing actor",
        sheet={"edition": "2014"},
    )

    same = profiles.set(campaign.id, edition="2014", locale="zh-CN")
    assert same.locale == "zh-CN"
    with pytest.raises(ValueError, match="explicit edition migration"):
        profiles.set(campaign.id, edition="2024")


def test_rule_pack_activation_receipt_is_atomic_with_campaign_revision(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Atomic rules")
    RuleProfileService(database).set(campaign.id, edition="2014")
    campaign = CampaignService(database).get(campaign.id)
    packs = RulePackService(database)
    manifest = _pack("dnd5e.atomic")
    packs.save_draft(manifest=manifest)
    packs.install(manifest["id"], "1.0.0")
    payload = {"pack_id": manifest["id"], "version": "1.0.0"}

    packs.set_activation(
        campaign.id,
        pack_id=manifest["id"],
        version="1.0.0",
        expected_campaign_revision=campaign.revision,
        idempotency_key="activate",
        idempotency_write=IdempotencyWrite(
            scope=f"rule-activation:{campaign.id}",
            payload=payload,
            response=lambda result: {
                "pack_id": result["activation"].pack_id,
                "fingerprint": result["effective"].fingerprint,
                "campaign_revision": result["campaign_revision"],
            },
        ),
    )

    replay = IdempotencyService(database).lookup(
        f"rule-activation:{campaign.id}",
        "activate",
        payload,
    )
    assert replay is not None
    assert replay.response["pack_id"] == manifest["id"]
    assert replay.response["campaign_revision"] == campaign.revision + 1


def test_rule_pack_activation_rolls_back_when_receipt_builder_fails(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Rollback rules")
    RuleProfileService(database).set(campaign.id, edition="2014")
    campaign = CampaignService(database).get(campaign.id)
    packs = RulePackService(database)
    manifest = _pack("dnd5e.rollback")
    packs.save_draft(manifest=manifest)
    packs.install(manifest["id"], "1.0.0")

    with pytest.raises(RuntimeError, match="receipt failed"):
        packs.set_activation(
            campaign.id,
            pack_id=manifest["id"],
            version="1.0.0",
            expected_campaign_revision=campaign.revision,
            idempotency_key="activate",
            idempotency_write=IdempotencyWrite(
                scope=f"rule-activation:{campaign.id}",
                payload={"pack_id": manifest["id"]},
                response=lambda _result: (_ for _ in ()).throw(RuntimeError("receipt failed")),
            ),
        )

    assert packs.effective_ruleset(campaign.id).lock == ()
    assert CampaignService(database).get(campaign.id).revision == campaign.revision


def test_rule_pack_rejects_unsafe_identity_and_missing_lock(database) -> None:
    packs = RulePackService(database)
    rejected = packs.save_draft(manifest={**_pack(), "id": "Bad"})
    assert rejected.status == "rejected"
    with pytest.raises(LookupError):
        packs.install("Bad", "1.0.0")

    self_dependent = packs.save_draft(
        manifest=_pack(
            "dnd5e.self-dependent",
            dependencies=["dnd5e.self-dependent"],
        )
    )
    assert self_dependent.status == "rejected"
    assert "a rule pack cannot depend on itself" in (self_dependent.validation_report["errors"])

    duplicate_dependency = packs.save_draft(
        manifest=_pack(
            "dnd5e.duplicate-dependency",
            dependencies=["dnd5e.base", {"id": "dnd5e.base"}],
        )
    )
    assert duplicate_dependency.status == "rejected"
    assert any(
        "duplicate rule-pack dependency" in error
        for error in duplicate_dependency.validation_report["errors"]
    )

    campaign = CampaignService(database).create(system_id="dnd5e", name="Unavailable")
    RuleProfileService(database).set(campaign.id, edition="2014")
    manifest = _pack("dnd5e.xgte2")
    packs.save_draft(manifest=manifest)
    packs.install("dnd5e.xgte2", "1.0.0")
    packs.set_activation(campaign.id, pack_id="dnd5e.xgte2", version="1.0.0")
    with database.transaction() as session:
        from sagasmith_core.models import RulePackVersion

        row = session.get(RulePackVersion, {"pack_id": "dnd5e.xgte2", "version": "1.0.0"})
        row.status = "validated"
    with pytest.raises(RulesetUnavailableError):
        packs.assert_edition_compatible(campaign.id, "2014")
    with pytest.raises(RulesetUnavailableError):
        packs.effective_ruleset(campaign.id)


def test_rule_pack_draft_identity_and_installed_status_are_safe(database) -> None:
    packs = RulePackService(database)
    rejected = packs.save_draft(manifest={"title": "Missing identity"})
    assert rejected.status == "rejected"
    assert packs.list_versions() == []

    manifest = _pack("dnd5e.stable")
    first = packs.save_draft(manifest=manifest)
    assert first.status == "validated"
    packs.install("dnd5e.stable", "1.0.0")
    repeated = packs.save_draft(manifest=manifest)
    assert repeated.status == "installed"
    unchanged = packs.save_draft(
        manifest=manifest,
        additional_errors=["a newer validator must not rewrite installed evidence"],
    )
    assert unchanged.validation_report["valid"] is True
    assert packs.get_version("dnd5e.stable", "1.0.0").status == "installed"


def test_rule_pack_provenance_is_version_scoped(database) -> None:
    packs = RulePackService(database)
    first = packs.save_draft(
        manifest=_pack("dnd5e.versioned-provenance"),
        provenance={"source": "first"},
    )
    second = packs.save_draft(
        manifest={
            **_pack("dnd5e.versioned-provenance"),
            "version": "2.0.0",
        },
        provenance={"source": "second"},
    )

    assert first.provenance == {"source": "first"}
    assert second.provenance == {"source": "second"}
    assert packs.provenance(first.pack_id, first.version) == {"source": "first"}
    assert packs.provenance(second.pack_id, second.version) == {"source": "second"}


def test_rule_pack_rejects_undeclared_events_and_unknown_artifact_refs(database) -> None:
    packs = RulePackService(database)
    rejected = packs.save_draft(
        manifest=_pack("dnd5e.invalid.refs"),
        mechanics=[
            {
                "id": "dnd5e.invalid.refs.rest",
                "event": "rest.after",
                "operations": [],
            }
        ],
        artifacts=[
            {
                "id": "dnd5e.invalid.refs.feature",
                "mechanic_refs": ["dnd5e.invalid.refs.missing"],
            }
        ],
    )
    assert rejected.status == "rejected"
    errors = rejected.validation_report["errors"]
    assert any("not declared" in item for item in errors)
    assert any("mechanic_refs are unknown" in item for item in errors)

    missing_capability = packs.save_draft(
        manifest={**_pack("dnd5e.invalid.capability"), "capabilities": []},
        mechanics=[
            {
                "id": "dnd5e.invalid.capability.rest",
                "event": "rest.after",
                "operations": [],
            }
        ],
    )
    assert missing_capability.status == "rejected"
    assert any("not declared" in item for item in missing_capability.validation_report["errors"])


def test_rule_pack_distinguishes_native_and_artifact_embedded_mechanics(
    database,
) -> None:
    packs = RulePackService(database)
    manifest = {
        **_pack("dnd5e.extension.contracts"),
        "native_mechanic_refs": ["dnd5e.core.spell.structured_resolution"],
    }
    draft = packs.save_draft(
        manifest=manifest,
        artifacts=[
            {
                "id": "dnd5e.extension.contracts.spell.spark",
                "kind": "spell",
                "card": {"name": "Spark"},
                "mechanic_refs": [
                    "dnd5e.core.spell.structured_resolution",
                    "dnd5e.extension.contracts.plan.spark",
                ],
                "embedded_mechanic_refs": ["dnd5e.extension.contracts.plan.spark"],
            }
        ],
    )

    assert draft.status == "validated"

    undeclared = packs.save_draft(
        manifest=_pack("dnd5e.extension.undeclared"),
        artifacts=[
            {
                "id": "dnd5e.extension.undeclared.spell.spark",
                "kind": "spell",
                "card": {"name": "Spark"},
                "mechanic_refs": ["dnd5e.core.spell.structured_resolution"],
            }
        ],
    )
    assert undeclared.status == "rejected"
    assert any(
        "mechanic_refs are unknown" in error for error in undeclared.validation_report["errors"]
    )


def test_effective_ruleset_rechecks_edition_after_profile_change(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Edition lock")
    RuleProfileService(database).set(campaign.id, edition="2014")
    packs = RulePackService(database)
    manifest = _pack("dnd5e.edition.lock")
    packs.save_draft(manifest=manifest)
    packs.install("dnd5e.edition.lock", "1.0.0")
    packs.set_activation(campaign.id, pack_id=manifest["id"], version="1.0.0")

    with pytest.raises(RulePackError, match="does not support"):
        packs.assert_edition_compatible(campaign.id, "2024")
    RuleProfileService(database).set(campaign.id, edition="2024")
    with pytest.raises(RulePackError, match="does not support"):
        packs.effective_ruleset(campaign.id)


def test_effective_ruleset_enforces_exact_dependency_checksum(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Dependency checksum")
    RuleProfileService(database).set(campaign.id, edition="2014")
    packs = RulePackService(database)
    dependency = packs.save_draft(manifest=_pack("dnd5e.dependency"))
    packs.install(dependency.pack_id, dependency.version)
    consumer_manifest = _pack(
        "dnd5e.consumer",
        dependencies=[
            {
                "id": dependency.pack_id,
                "version": dependency.version,
                "checksum": "f" * 64,
            }
        ],
    )
    consumer = packs.save_draft(manifest=consumer_manifest)
    packs.install(consumer.pack_id, consumer.version)
    packs.set_activation(
        campaign.id,
        pack_id=dependency.pack_id,
        version=dependency.version,
    )

    with pytest.raises(RulePackError, match="requires checksum"):
        packs.set_activation(
            campaign.id,
            pack_id=consumer.pack_id,
            version=consumer.version,
        )

    assert [item["pack_id"] for item in packs.effective_ruleset(campaign.id).lock] == [
        dependency.pack_id
    ]


def test_effective_ruleset_accepts_stable_dependency_definition_checksum(
    database,
) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Portable dependency")
    RuleProfileService(database).set(campaign.id, edition="2014")
    packs = RulePackService(database)
    definition_checksum = "a" * 64
    dependency = packs.save_draft(
        manifest=_pack("dnd5e.portable.dependency"),
        provenance={
            "content_definition": {
                "checksum": "b" * 64,
                "definition_checksum": definition_checksum,
            }
        },
    )
    packs.install(dependency.pack_id, dependency.version)
    consumer = packs.save_draft(
        manifest=_pack(
            "dnd5e.portable.consumer",
            dependencies=[
                {
                    "id": dependency.pack_id,
                    "version": dependency.version,
                    "checksum": definition_checksum,
                }
            ],
        )
    )
    packs.install(consumer.pack_id, consumer.version)
    packs.set_activation(
        campaign.id,
        pack_id=dependency.pack_id,
        version=dependency.version,
    )
    packs.set_activation(
        campaign.id,
        pack_id=consumer.pack_id,
        version=consumer.version,
    )

    assert {item["pack_id"] for item in packs.effective_ruleset(campaign.id).lock} == {
        dependency.pack_id,
        consumer.pack_id,
    }
