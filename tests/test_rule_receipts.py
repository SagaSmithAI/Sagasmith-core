from __future__ import annotations

import pytest

from sagasmith_core import (
    BranchService,
    CampaignService,
    RevisionService,
    RuleReceiptService,
    SnapshotService,
    StateMutationService,
)


def _write_receipt(database, campaign_id: str, *, event: str = "feature.applied") -> None:
    StateMutationService(database).replace(
        campaign_id,
        campaign_state={"phase": "resolved"},
        operation="test.receipt",
        rule_receipts=[
            {
                "event": event,
                "mechanic_id": "dnd5e.core.feature",
                "ruleset_fingerprint": "f" * 64,
                "character_id": "character-1",
                "artifact_id": "artifact-1",
                "pack_id": "pack-1",
                "pack_version": "1.0.0",
            }
        ],
    )


def test_has_applied_receipt_matches_exact_subset_and_branch(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Receipts")
    _write_receipt(database, campaign.id)
    service = RuleReceiptService(database)
    fields = {"character_id": "character-1", "pack_version": "1.0.0"}

    assert service.has_applied_receipt(
        campaign.id, event="feature.applied", receipt_fields=fields
    )
    assert fields == {"character_id": "character-1", "pack_version": "1.0.0"}
    assert not service.has_applied_receipt(
        campaign.id,
        event="feature.applied",
        receipt_fields={"character_id": "other"},
    )
    assert not service.has_applied_receipt(
        campaign.id,
        event="feature.applied",
        receipt_fields={"missing": None},
    )

    snapshot = SnapshotService(database).create(campaign.id, label="receipt")
    fork = BranchService(database).create(
        campaign.id, name="receipt-fork", from_snapshot_id=snapshot.id
    )
    assert service.has_applied_receipt(
        campaign.id,
        branch_id=fork.id,
        event="feature.applied",
        receipt_fields={"artifact_id": "artifact-1"},
    )


def test_has_applied_receipt_excludes_undone_groups(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Receipts")
    _write_receipt(database, campaign.id)
    service = RuleReceiptService(database)
    assert service.has_applied_receipt(
        campaign.id,
        event="feature.applied",
        receipt_fields={"pack_id": "pack-1"},
    )
    RevisionService(database).undo(campaign.id)
    assert not service.has_applied_receipt(
        campaign.id,
        event="feature.applied",
        receipt_fields={"pack_id": "pack-1"},
    )


@pytest.mark.parametrize("value", [{}, [], None, "receipt"])
def test_has_applied_receipt_requires_nonempty_mapping(database, value) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Receipts")
    with pytest.raises(ValueError, match="non-empty mapping"):
        RuleReceiptService(database).has_applied_receipt(
            campaign.id, event="feature.applied", receipt_fields=value
        )

