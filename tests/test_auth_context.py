from datetime import UTC, datetime, timedelta

import pytest

from sagasmith_core.auth_context import (
    AUTH_CONTEXT_SCHEMA,
    AuthContextNonceGuard,
    sign_auth_context,
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
