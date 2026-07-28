"""Canonical serialization and hashing for persisted integrity contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Encode JSON deterministically for checksums and idempotency identities."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_sha256(value: Any) -> str:
    """Hash a value through the one canonical JSON encoding."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
