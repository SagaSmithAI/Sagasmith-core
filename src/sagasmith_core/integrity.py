"""Canonical serialization and hashing for persisted integrity contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from typing import Any


def canonical_json(value: Any) -> str:
    """Encode JSON deterministically for checksums and idempotency identities."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_sha256(value: Any) -> str:
    """Hash a value through the one canonical JSON encoding."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sign_canonical_envelope(payload: Mapping[str, Any], secret: bytes) -> dict[str, Any]:
    """Return a canonical JSON/HMAC-SHA256 envelope."""

    value = dict(payload)
    value["signature"] = hmac.new(
        secret,
        canonical_json(value).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return value


def verify_canonical_envelope(
    envelope: Any,
    secret: bytes,
    *,
    missing_error: str,
    invalid_error: str,
) -> dict[str, Any]:
    """Verify and unwrap a canonical JSON/HMAC-SHA256 envelope."""

    if not isinstance(envelope, dict):
        raise ValueError(missing_error)
    payload = dict(envelope)
    signature = str(payload.pop("signature", ""))
    expected = hmac.new(
        secret,
        canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError(invalid_error)
    return payload


def unique_retired_source_key(
    source_key: str,
    checksum: str,
    *,
    exists: Callable[[str], bool],
    maximum_length: int = 200,
) -> str:
    """Return the canonical collision-safe key for an immutable source revision."""

    suffix_room = 20
    stem = f"{str(source_key)[: maximum_length - suffix_room]}@{str(checksum)[:12]}"
    candidate = stem
    suffix = 2
    while exists(candidate):
        suffix_text = f"-{suffix}"
        candidate = f"{stem[: maximum_length - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return candidate
