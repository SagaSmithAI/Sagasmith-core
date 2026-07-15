import pytest

from sagasmith_core import CampaignService, RulePackService, RuleProfileService, SnapshotService
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
        artifacts=[{"id": "dnd5e.xgte.feature.test", "kind": "feature"}],
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
    activation = packs.set_activation(
        campaign.id, pack_id="dnd5e.xgte", version="1.0.0"
    )
    effective = packs.effective_ruleset(campaign.id)
    assert effective.lock[0]["checksum"] == activation.checksum
    assert effective.mechanics[0]["id"] == "dnd5e.xgte.mechanic.test"

    snapshot = SnapshotService(database).create(campaign.id, label="with rules")
    fork = BranchService(database).create(
        campaign.id, name="without rules", from_snapshot_id=snapshot.id, checkout=True
    )
    assert (
        packs.effective_ruleset(campaign.id, branch_id=fork.id).fingerprint
        == effective.fingerprint
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


def test_rule_pack_rejects_unsafe_identity_and_missing_lock(database) -> None:
    packs = RulePackService(database)
    rejected = packs.save_draft(manifest={**_pack(), "id": "Bad"})
    assert rejected.status == "rejected"
    with pytest.raises(LookupError):
        packs.install("Bad", "1.0.0")

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
    assert any(
        "not declared" in item
        for item in missing_capability.validation_report["errors"]
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
