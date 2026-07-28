"""Operational wall-clock authority shared by persistence and service leases."""

from __future__ import annotations

from datetime import UTC, datetime


def operational_utcnow() -> datetime:
    """Return the timezone-aware UTC wall clock for operational metadata."""

    return datetime.now(UTC)
