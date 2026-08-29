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
AUTH_CONTEXT_DELEGATION_SCHEMA = "sagasmith.auth-context/v2"
AUTH_CONTEXT_META_KEY = "sagasmith_auth_context"
AUTH_CONTEXT_RECEIPT_META_KEY = "sagasmith_auth_context_receipt"
_MAX_AGE = timedelta(minutes=5)
_V1_FIELDS = frozenset(
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
_V2_FIELDS = frozenset(
    {
        "schema",
        "issuer",
        "target_service",
        "caller_principal",
        "workload_identity",
        "requester_principal",
        "resource_owner_principal",
        "acting_host_principal",
        "acting_character_id",
        "authorized_audience",
        "allowed_operations",
        "conversation_principal",
        "tenant_id",
        "campaign_id",
        "room_turn_id",
        "base_revision",
        "principal_source",
        "issued_at",
        "expires_at",
        "nonce",
        "signature",
    }
)
_MAX_DELEGATION_TTL = timedelta(minutes=15)


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


def _non_negative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _allowed_operations(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("allowed_operations must be a non-empty list")
    operations = tuple(_required_text(item, "allowed_operation", maximum=100) for item in value)
    if len(operations) > 100:
        raise ValueError("allowed_operations contains too many entries")
    if len(set(operations)) != len(operations):
        raise ValueError("allowed_operations must not contain duplicates")
    if "*" in operations:
        raise ValueError("allowed_operations must enumerate concrete operations")
    return tuple(sorted(operations))


@dataclass(frozen=True)
class AuthContext:
    """One verified caller/conversation binding supplied outside model arguments.

    ``actor_principal`` is the authority that executes an operation. It is the
    legacy caller for v1 and the acting Host for v2. ``authorization_principal``
    remains the human requester whose campaign role is evaluated.
    """

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
    target_service: str = ""
    caller_principal: str = ""
    workload_identity: str = ""
    requester_principal: str = ""
    resource_owner_principal: str = ""
    acting_host_principal: str = ""
    acting_character_id: str = ""
    authorized_audience: str = ""
    allowed_operations: tuple[str, ...] = ()
    room_turn_id: str = ""
    base_revision: int = 0
    expires_at: datetime | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AuthContext":
        schema = value.get("schema")
        fields = (
            _V1_FIELDS
            if schema == AUTH_CONTEXT_SCHEMA
            else _V2_FIELDS
            if schema == AUTH_CONTEXT_DELEGATION_SCHEMA
            else None
        )
        if fields is None:
            raise ValueError("unsupported auth context schema")
        if set(value) != fields:
            missing = sorted(fields - set(value))
            extra = sorted(set(value) - fields)
            raise ValueError(
                f"auth context fields do not match {schema} (missing={missing}, extra={extra})"
            )
        principal_source = _required_text(value.get("principal_source"), "principal_source")
        if principal_source != "trusted-host":
            raise ValueError("principal_source must be trusted-host")
        signature = _required_text(value.get("signature"), "signature", maximum=64).casefold()
        if len(signature) != 64 or any(
            character not in "0123456789abcdef" for character in signature
        ):
            raise ValueError("signature must be a lowercase HMAC-SHA256 digest")
        if schema == AUTH_CONTEXT_SCHEMA:
            epoch = _non_negative_integer(value.get("authorization_epoch"), "authorization_epoch")
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
        issued_at = _parse_issued_at(value.get("issued_at"))
        expires_at = _parse_issued_at(value.get("expires_at"))
        if expires_at <= issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if expires_at - issued_at > _MAX_DELEGATION_TTL:
            raise ValueError("delegated auth context lifetime exceeds 15 minutes")
        requester = _required_text(value.get("requester_principal"), "requester_principal")
        acting_host = _required_text(
            value.get("acting_host_principal"), "acting_host_principal"
        )
        room_turn_id = _required_text(value.get("room_turn_id"), "room_turn_id")
        base_revision = _non_negative_integer(value.get("base_revision"), "base_revision")
        issuer = _required_text(value.get("issuer"), "issuer")
        return cls(
            host=issuer,
            channel="hosted",
            actor_principal=acting_host,
            conversation_principal=_required_text(
                value.get("conversation_principal"), "conversation_principal"
            ),
            tenant_id=_optional_text(value.get("tenant_id"), "tenant_id"),
            campaign_id=_required_text(value.get("campaign_id"), "campaign_id"),
            session_id=room_turn_id,
            principal_source=principal_source,
            authorization_epoch=base_revision,
            issued_at=issued_at,
            nonce=_required_text(value.get("nonce"), "nonce", maximum=128),
            signature=signature,
            schema=AUTH_CONTEXT_DELEGATION_SCHEMA,
            target_service=_required_text(value.get("target_service"), "target_service"),
            caller_principal=_required_text(value.get("caller_principal"), "caller_principal"),
            workload_identity=_required_text(value.get("workload_identity"), "workload_identity"),
            requester_principal=requester,
            resource_owner_principal=_required_text(
                value.get("resource_owner_principal"), "resource_owner_principal"
            ),
            acting_host_principal=acting_host,
            acting_character_id=_optional_text(
                value.get("acting_character_id"), "acting_character_id"
            ),
            authorized_audience=_required_text(
                value.get("authorized_audience"), "authorized_audience"
            ),
            allowed_operations=_allowed_operations(value.get("allowed_operations")),
            room_turn_id=room_turn_id,
            base_revision=base_revision,
            expires_at=expires_at,
        )

    @property
    def authority_principal(self) -> str:
        """Return the principal that performs the authoritative operation."""

        return self.actor_principal

    @property
    def authorization_principal(self) -> str:
        """Return the human/service requester whose campaign access is checked."""

        return self.requester_principal or self.actor_principal

    def unsigned_payload(self) -> dict[str, Any]:
        if self.schema == AUTH_CONTEXT_DELEGATION_SCHEMA:
            assert self.expires_at is not None
            return {
                "schema": self.schema,
                "issuer": self.host,
                "target_service": self.target_service,
                "caller_principal": self.caller_principal,
                "workload_identity": self.workload_identity,
                "requester_principal": self.requester_principal,
                "resource_owner_principal": self.resource_owner_principal,
                "acting_host_principal": self.acting_host_principal,
                "acting_character_id": self.acting_character_id,
                "authorized_audience": self.authorized_audience,
                "allowed_operations": list(self.allowed_operations),
                "conversation_principal": self.conversation_principal,
                "tenant_id": self.tenant_id,
                "campaign_id": self.campaign_id,
                "room_turn_id": self.room_turn_id,
                "base_revision": self.base_revision,
                "principal_source": self.principal_source,
                "issued_at": self.issued_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
                "nonce": self.nonce,
            }
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

        if self.schema == AUTH_CONTEXT_DELEGATION_SCHEMA:
            assert self.expires_at is not None
            return {
                "schema": self.schema,
                "issuer": self.host,
                "target_service": self.target_service,
                "caller_principal": self.caller_principal,
                "workload_identity": self.workload_identity,
                "requester_principal": self.requester_principal,
                "resource_owner_principal": self.resource_owner_principal,
                "acting_host_principal": self.acting_host_principal,
                "acting_character_id": self.acting_character_id,
                "authorized_audience": self.authorized_audience,
                "allowed_operations": list(self.allowed_operations),
                "conversation_principal": self.conversation_principal,
                "tenant_id": self.tenant_id,
                "campaign_id": self.campaign_id,
                "room_turn_id": self.room_turn_id,
                "base_revision": self.base_revision,
                "expires_at": self.expires_at.isoformat(),
                "tool": _required_text(tool, "tool"),
                "revision": revision,
                "nonce": self.nonce,
            }
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


def sign_delegated_auth_context(
    *,
    secret: bytes | str,
    issuer: str,
    target_service: str,
    caller_principal: str,
    workload_identity: str,
    requester_principal: str,
    resource_owner_principal: str,
    acting_host_principal: str,
    authorized_audience: str,
    allowed_operations: list[str] | tuple[str, ...],
    conversation_principal: str,
    campaign_id: str,
    room_turn_id: str,
    base_revision: int,
    acting_character_id: str = "",
    tenant_id: str = "",
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Issue a short-lived, audience-bound delegation for one Host turn."""

    now = (issued_at or datetime.now(UTC)).astimezone(UTC)
    expiry = (expires_at or now + _MAX_AGE).astimezone(UTC)
    operations = _allowed_operations(list(allowed_operations))
    payload = {
        "schema": AUTH_CONTEXT_DELEGATION_SCHEMA,
        "issuer": _required_text(issuer, "issuer"),
        "target_service": _required_text(target_service, "target_service"),
        "caller_principal": _required_text(caller_principal, "caller_principal"),
        "workload_identity": _required_text(workload_identity, "workload_identity"),
        "requester_principal": _required_text(requester_principal, "requester_principal"),
        "resource_owner_principal": _required_text(
            resource_owner_principal, "resource_owner_principal"
        ),
        "acting_host_principal": _required_text(
            acting_host_principal, "acting_host_principal"
        ),
        "acting_character_id": _optional_text(acting_character_id, "acting_character_id"),
        "authorized_audience": _required_text(authorized_audience, "authorized_audience"),
        "allowed_operations": list(operations),
        "conversation_principal": _required_text(
            conversation_principal, "conversation_principal"
        ),
        "tenant_id": _optional_text(tenant_id, "tenant_id"),
        "campaign_id": _required_text(campaign_id, "campaign_id"),
        "room_turn_id": _required_text(room_turn_id, "room_turn_id"),
        "base_revision": _non_negative_integer(base_revision, "base_revision"),
        "principal_source": "trusted-host",
        "issued_at": now.isoformat(),
        "expires_at": expiry.isoformat(),
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
    expected_service: str | None = None,
    expected_operation: str | None = None,
    expected_audience: str | None = None,
    expected_room_turn: str | None = None,
    expected_base_revision: int | None = None,
    expected_resource_owner: str | None = None,
    expected_acting_character: str | None = None,
    expected_requester: str | None = None,
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
    if context.schema == AUTH_CONTEXT_DELEGATION_SCHEMA:
        assert context.expires_at is not None
        if current >= context.expires_at:
            raise ValueError("auth context has expired")
    elif current - context.issued_at > max_age:
        raise ValueError("auth context has expired")
    if expected_actor is not None and context.actor_principal != expected_actor:
        raise ValueError("auth context actor does not match the tool caller")
    if expected_campaign is not None and context.campaign_id != expected_campaign:
        raise ValueError("auth context campaign does not match the tool call")
    if expected_session is not None and context.session_id != expected_session:
        raise ValueError("auth context session does not match the MCP session")
    modern_expectations = any(
        value is not None
        for value in (
            expected_service,
            expected_operation,
            expected_audience,
            expected_room_turn,
            expected_base_revision,
            expected_resource_owner,
            expected_acting_character,
            expected_requester,
        )
    )
    if modern_expectations and context.schema != AUTH_CONTEXT_DELEGATION_SCHEMA:
        raise ValueError("delegated auth context v2 is required")
    if expected_service is not None and context.target_service != expected_service:
        raise ValueError("auth context target service does not match this MCP server")
    if expected_operation is not None and expected_operation not in context.allowed_operations:
        raise ValueError("auth context does not allow this operation")
    if expected_audience is not None and context.authorized_audience != expected_audience:
        raise ValueError("auth context audience does not match the tool call")
    if expected_room_turn is not None and context.room_turn_id != expected_room_turn:
        raise ValueError("auth context room turn does not match the tool call")
    if expected_base_revision is not None and context.base_revision != expected_base_revision:
        raise ValueError("auth context base revision is stale")
    if (
        expected_resource_owner is not None
        and context.resource_owner_principal != expected_resource_owner
    ):
        raise ValueError("auth context resource owner does not match the tool call")
    if (
        expected_acting_character is not None
        and context.acting_character_id != expected_acting_character
    ):
        raise ValueError("auth context acting character does not match the tool call")
    if (
        expected_requester is not None
        and context.requester_principal != expected_requester
    ):
        raise ValueError("auth context requester does not match the tool caller")
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
        issuer = context.workload_identity or context.host
        key = f"{issuer}:{context.nonce}"
        with self._lock:
            for nonce in [item for item, timestamp in self._seen.items() if timestamp < cutoff]:
                self._seen.pop(nonce, None)
            if key in self._seen:
                raise ValueError("auth context nonce was already used")
            if len(self._seen) >= self.maximum_entries:
                raise RuntimeError("auth context replay guard is at capacity")
            self._seen[key] = current
