"""Signed, system-neutral Host identity envelopes for authoritative calls."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sagasmith_core.integrity import canonical_json

AUTH_CONTEXT_SCHEMA = "sagasmith.auth-context/v1"
AUTH_CONTEXT_META_KEY = "sagasmith_auth_context"
AUTH_CONTEXT_RECEIPT_META_KEY = "sagasmith_auth_context_receipt"
_MAX_AGE = timedelta(minutes=5)
_FIELDS = frozenset(
    {
        "schema",
        "host",
        "channel",
        "actor_principal",
        "conversation_principal",
        "tenant_id",
        "campaign_id",
        "session_id",
        "principal_source",
        "authorization_epoch",
        "issued_at",
        "nonce",
        "signature",
    }
)


def _required_text(value: Any, field: str, *, maximum: int = 300) -> str:
    if not isinstance(value, str) or not (result := value.strip()):
        raise ValueError(f"{field} is required")
    if len(result) > maximum:
        raise ValueError(f"{field} is too long")
    return result


def _optional_text(value: Any, field: str, *, maximum: int = 300) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{field} is too long")
    return result


def _secret_bytes(secret: bytes | str) -> bytes:
    value = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("auth context secret must contain at least 32 bytes")
    return value


def _parse_issued_at(value: Any) -> datetime:
    text = _required_text(value, "issued_at", maximum=64)
    try:
        issued_at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("issued_at must be an ISO-8601 timestamp") from exc
    if issued_at.tzinfo is None:
        raise ValueError("issued_at must be timezone-aware")
    return issued_at.astimezone(UTC)


@dataclass(frozen=True)
class AuthContext:
    """One verified caller/conversation binding supplied outside model arguments."""

    host: str
    channel: str
    actor_principal: str
    conversation_principal: str
    tenant_id: str
    campaign_id: str
    session_id: str
    principal_source: str
    authorization_epoch: int
    issued_at: datetime
    nonce: str
    signature: str
    schema: str = AUTH_CONTEXT_SCHEMA

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AuthContext":
        if set(value) != _FIELDS:
            missing = sorted(_FIELDS - set(value))
            extra = sorted(set(value) - _FIELDS)
            raise ValueError(
                f"auth context fields do not match v1 (missing={missing}, extra={extra})"
            )
        if value.get("schema") != AUTH_CONTEXT_SCHEMA:
            raise ValueError("unsupported auth context schema")
        epoch = value.get("authorization_epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("authorization_epoch must be a non-negative integer")
        principal_source = _required_text(value.get("principal_source"), "principal_source")
        if principal_source != "trusted-host":
            raise ValueError("principal_source must be trusted-host")
        signature = _required_text(value.get("signature"), "signature", maximum=64).casefold()
        if len(signature) != 64 or any(
            character not in "0123456789abcdef" for character in signature
        ):
            raise ValueError("signature must be a lowercase HMAC-SHA256 digest")
        return cls(
            host=_required_text(value.get("host"), "host"),
            channel=_required_text(value.get("channel"), "channel"),
            actor_principal=_required_text(value.get("actor_principal"), "actor_principal"),
            conversation_principal=_required_text(
                value.get("conversation_principal"), "conversation_principal"
            ),
            tenant_id=_optional_text(value.get("tenant_id"), "tenant_id"),
            campaign_id=_optional_text(value.get("campaign_id"), "campaign_id"),
            session_id=_required_text(value.get("session_id"), "session_id"),
            principal_source=principal_source,
            authorization_epoch=epoch,
            issued_at=_parse_issued_at(value.get("issued_at")),
            nonce=_required_text(value.get("nonce"), "nonce", maximum=128),
            signature=signature,
        )

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "host": self.host,
            "channel": self.channel,
            "actor_principal": self.actor_principal,
            "conversation_principal": self.conversation_principal,
            "tenant_id": self.tenant_id,
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "principal_source": self.principal_source,
            "authorization_epoch": self.authorization_epoch,
            "issued_at": self.issued_at.isoformat(),
            "nonce": self.nonce,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "signature": self.signature}

    def audit_receipt(self, *, tool: str, revision: int | str | None = None) -> dict[str, Any]:
        """Return the identity fields every authoritative write can retain."""

        return {
            "schema": AUTH_CONTEXT_SCHEMA,
            "actor_principal": self.actor_principal,
            "conversation_principal": self.conversation_principal,
            "tenant_id": self.tenant_id,
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "tool": _required_text(tool, "tool"),
            "authorization_epoch": self.authorization_epoch,
            "revision": revision,
            "nonce": self.nonce,
        }


def sign_auth_context(
    *,
    secret: bytes | str,
    host: str,
    channel: str,
    actor_principal: str,
    conversation_principal: str,
    session_id: str,
    tenant_id: str = "",
    campaign_id: str = "",
    authorization_epoch: int = 0,
    issued_at: datetime | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Build a canonical signed v1 envelope for MCP request metadata."""

    now = (issued_at or datetime.now(UTC)).astimezone(UTC)
    payload = {
        "schema": AUTH_CONTEXT_SCHEMA,
        "host": _required_text(host, "host"),
        "channel": _required_text(channel, "channel"),
        "actor_principal": _required_text(actor_principal, "actor_principal"),
        "conversation_principal": _required_text(
            conversation_principal, "conversation_principal"
        ),
        "tenant_id": _optional_text(tenant_id, "tenant_id"),
        "campaign_id": _optional_text(campaign_id, "campaign_id"),
        "session_id": _required_text(session_id, "session_id"),
        "principal_source": "trusted-host",
        "authorization_epoch": authorization_epoch,
        "issued_at": now.isoformat(),
        "nonce": nonce or secrets.token_urlsafe(24),
    }
    unsigned = AuthContext.from_mapping({**payload, "signature": "0" * 64}).unsigned_payload()
    signature = hmac.new(
        _secret_bytes(secret), canonical_json(unsigned).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {**unsigned, "signature": signature}


def verify_auth_context(
    envelope: Any,
    secret: bytes | str,
    *,
    now: datetime | None = None,
    max_age: timedelta = _MAX_AGE,
    expected_actor: str | None = None,
    expected_campaign: str | None = None,
    expected_session: str | None = None,
) -> AuthContext:
    """Verify signature, freshness, and any server-owned call bindings."""

    if not isinstance(envelope, Mapping):
        raise ValueError("signed auth context is required")
    context = AuthContext.from_mapping(envelope)
    expected_signature = hmac.new(
        _secret_bytes(secret),
        canonical_json(context.unsigned_payload()).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(context.signature, expected_signature):
        raise ValueError("auth context signature is invalid")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if context.issued_at > current + timedelta(seconds=30):
        raise ValueError("auth context was issued in the future")
    if current - context.issued_at > max_age:
        raise ValueError("auth context has expired")
    if expected_actor is not None and context.actor_principal != expected_actor:
        raise ValueError("auth context actor does not match the tool caller")
    if expected_campaign is not None and context.campaign_id != expected_campaign:
        raise ValueError("auth context campaign does not match the tool call")
    if expected_session is not None and context.session_id != expected_session:
        raise ValueError("auth context session does not match the MCP session")
    return context


class AuthContextNonceGuard:
    """Bounded in-memory replay rejection for short-lived signed envelopes."""

    def __init__(self, *, retention: timedelta = _MAX_AGE, maximum_entries: int = 100_000) -> None:
        self.retention = retention
        self.maximum_entries = maximum_entries
        self._seen: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def remember(self, context: AuthContext, *, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = current - self.retention
        key = f"{context.host}:{context.nonce}"
        with self._lock:
            for nonce in [item for item, timestamp in self._seen.items() if timestamp < cutoff]:
                self._seen.pop(nonce, None)
            if key in self._seen:
                raise ValueError("auth context nonce was already used")
            if len(self._seen) >= self.maximum_entries:
                raise RuntimeError("auth context replay guard is at capacity")
            self._seen[key] = current
