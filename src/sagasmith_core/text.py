"""Small deterministic text keys shared across SagaSmith layers."""

from __future__ import annotations

import re
from typing import Any

_NON_ASCII_ALPHANUMERIC_RE = re.compile(r"[^a-z0-9]+")


def compact_ascii_key(value: Any) -> str:
    """Case-fold text and retain only ASCII letters and digits."""

    return _NON_ASCII_ALPHANUMERIC_RE.sub("", str(value or "").casefold())


def ascii_slug(value: Any) -> str:
    """Case-fold text into an ASCII lowercase hyphen-separated slug."""

    return _NON_ASCII_ALPHANUMERIC_RE.sub("-", str(value or "").casefold()).strip("-")
