from datetime import UTC, datetime, timedelta

import pytest

from sagasmith_core.auth_context import (
    AUTH_CONTEXT_DELEGATION_SCHEMA,
    AUTH_CONTEXT_SCHEMA,
    AuthContextNonceGuard,
    sign_auth_context,
    sign_delegated_auth_context,
    verify_auth_context,
)

SECRET = "s" * 32
NOW = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)


def signed(**overrides):
    values = {
        "secret": SECRET,
        "host": "sagasmith-agent",
        "channel": "discord",
        "actor_principal": "discord:user:123",
        "conversation_principal": "discord:group:456",
        "tenant_id": "tenant-1",
        "campaign_id": "campaign-9",
        "session_id": "session-18",
        "authorization_epoch": 17,
        "issued_at": NOW,
        "nonce": "nonce-1",
    }
    values.update(overrides)
    return sign_auth_context(**values)


def test_signed_auth_context_round_trip_and_audit_receipt() -> None:
    envelope = signed()
    assert envelope["schema"] == AUTH_CONTEXT_SCHEMA

    context = verify_auth_context(
        envelope,
        SECRET,
        now=NOW,
        expected_actor="discord:user:123",
        expected_campaign="campaign-9",
        expected_session="session-18",
    )

    assert context.actor_principal == "discord:user:123"
    assert context.authority_principal == "discord:user:123"
    assert context.authorization_principal == "discord:user:123"

    assert context.conversation_principal == "discord:group:456"
    assert context.audit_receipt(tool="combat_start", revision=8) == {
        "schema": AUTH_CONTEXT_SCHEMA,
        "actor_principal": "discord:user:123",
        "conversation_principal": "discord:group:456",
        "tenant_id": "tenant-1",
        "campaign_id": "campaign-9",
        "session_id": "session-18",
        "tool": "combat_start",
        "authorization_epoch": 17,
        "revision": 8,
        "nonce": "nonce-1",
    }


def test_verification_rejects_tampering_expiry_and_actor_mismatch() -> None:
    tampered = {**signed(), "actor_principal": "discord:user:attacker"}
    with pytest.raises(ValueError, match="signature"):
        verify_auth_context(tampered, SECRET, now=NOW)
    with pytest.raises(ValueError, match="expired"):
        verify_auth_context(signed(), SECRET, now=NOW + timedelta(minutes=6))
    with pytest.raises(ValueError, match="actor"):
        verify_auth_context(signed(), SECRET, now=NOW, expected_actor="discord:user:other")
    with pytest.raises(ValueError, match="campaign"):
        verify_auth_context(signed(), SECRET, now=NOW, expected_campaign="campaign-other")
    with pytest.raises(ValueError, match="session"):
        verify_auth_context(signed(), SECRET, now=NOW, expected_session="session-other")


def test_nonce_guard_rejects_replay_without_blocking_another_actor() -> None:
    guard = AuthContextNonceGuard()
    first = verify_auth_context(signed(), SECRET, now=NOW)
    second = verify_auth_context(
        signed(actor_principal="discord:user:999", nonce="nonce-2"), SECRET, now=NOW
    )
    guard.remember(first, now=NOW)
    guard.remember(second, now=NOW)
    with pytest.raises(ValueError, match="already used"):
        guard.remember(first, now=NOW)


def delegated(**overrides):
    values = {
        "secret": SECRET,
        "issuer": "sagasmith-web",
        "target_service": "sagasmith-coc-mcp",
        "caller_principal": "workload:web:room-worker",
        "workload_identity": "spiffe://sagasmith/web/room-worker",
        "requester_principal": "discord:user:123",
        "resource_owner_principal": "discord:user:owner",
        "acting_host_principal": "campaign:keeper",
        "acting_character_id": "investigator-7",
        "authorized_audience": "player",
        "allowed_operations": ["campaign_query", "investigation_check"],
        "conversation_principal": "discord:group:456",
        "tenant_id": "tenant-1",
        "campaign_id": "campaign-9",
        "room_turn_id": "turn-18",
        "base_revision": 17,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
        "nonce": "delegation-1",
    }
    values.update(overrides)
    return sign_delegated_auth_context(**values)


def test_delegated_auth_context_round_trip_and_receipt() -> None:
    envelope = delegated()
    assert envelope["schema"] == AUTH_CONTEXT_DELEGATION_SCHEMA
    assert envelope["allowed_operations"] == ["campaign_query", "investigation_check"]

    context = verify_auth_context(
        envelope,
        SECRET,
        now=NOW,
        expected_actor="campaign:keeper",
        expected_requester="discord:user:123",
        expected_campaign="campaign-9",
        expected_service="sagasmith-coc-mcp",
        expected_operation="investigation_check",
        expected_audience="player",
        expected_room_turn="turn-18",
        expected_base_revision=17,
        expected_resource_owner="discord:user:owner",
        expected_acting_character="investigator-7",
    )

    assert context.actor_principal == "campaign:keeper"
    assert context.authority_principal == "campaign:keeper"
    assert context.authorization_principal == "discord:user:123"

    receipt = context.audit_receipt(tool="investigation_check", revision=18)
    assert receipt["workload_identity"] == "spiffe://sagasmith/web/room-worker"
    assert receipt["room_turn_id"] == "turn-18"
    assert receipt["base_revision"] == 17
    assert receipt["revision"] == 18


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"expected_actor": "discord:user:123"}, "actor"),
        ({"expected_service": "sagasmith-dnd-mcp"}, "target service"),
        ({"expected_operation": "combat_start"}, "operation"),
        ({"expected_audience": "dm"}, "audience"),
        ({"expected_room_turn": "turn-other"}, "room turn"),
        ({"expected_base_revision": 16}, "revision"),
        ({"expected_resource_owner": "discord:user:other"}, "resource owner"),
        ({"expected_acting_character": "investigator-other"}, "acting character"),
        ({"expected_requester": "discord:user:other"}, "requester"),
    ],
)
def test_delegated_auth_context_rejects_wrong_authority_binding(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        verify_auth_context(delegated(), SECRET, now=NOW, **kwargs)


def test_delegated_auth_context_rejects_expiry_and_excessive_lifetime() -> None:
    with pytest.raises(ValueError, match="expired"):
        verify_auth_context(
            delegated(),
            SECRET,
            now=NOW + timedelta(minutes=5),
        )
    with pytest.raises(ValueError, match="15 minutes"):
        delegated(expires_at=NOW + timedelta(minutes=16))


def test_legacy_context_cannot_satisfy_modern_authority_checks() -> None:
    with pytest.raises(ValueError, match="v2 is required"):
        verify_auth_context(
            signed(),
            SECRET,
            now=NOW,
            expected_service="sagasmith-coc-mcp",
        )


def test_delegation_rejects_wildcards_and_token_passthrough_fields() -> None:
    with pytest.raises(ValueError, match="concrete operations"):
        delegated(allowed_operations=["*"])

    envelope = {**delegated(), "access_token": "must-not-cross-the-boundary"}
    with pytest.raises(ValueError, match="fields do not match"):
        verify_auth_context(envelope, SECRET, now=NOW)
