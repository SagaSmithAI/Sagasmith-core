"""Canonical serialization and hashing for persisted integrity contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any


def canonical_json(value: Any) -> str:
    """Encode JSON deterministically for checksums and idempotency identities."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_sha256(value: Any) -> str:
    """Hash a value through the one canonical JSON encoding."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


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
