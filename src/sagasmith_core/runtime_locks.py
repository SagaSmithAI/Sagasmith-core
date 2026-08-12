"""System-neutral runtime mutation locks declared by an authoritative host."""

from __future__ import annotations

from typing import Any, Mapping


def mutation_lock(state: Mapping[str, Any] | None, domain: str) -> dict[str, Any] | None:
    """Return the active lock for a configuration domain, if one is declared."""

    locks = dict(state or {}).get("mutation_locks", [])
    if not isinstance(locks, list):
        raise ValueError("campaign state mutation_locks must be an array")
    for index, raw in enumerate(locks):
        if not isinstance(raw, Mapping):
            raise ValueError(f"campaign state mutation_locks[{index}] must be an object")
        domains = raw.get("domains")
        if not isinstance(domains, list) or any(not isinstance(item, str) for item in domains):
            raise ValueError(f"campaign state mutation_locks[{index}].domains is invalid")
        if domain in domains:
            return dict(raw)
    return None


def require_mutation_unlocked(
    state: Mapping[str, Any] | None,
    domain: str,
    *,
    error_type: type[Exception] = ValueError,
) -> None:
    lock = mutation_lock(state, domain)
    if lock is None:
        return
    reason = str(lock.get("reason") or "active runtime activity")
    raise error_type(f"{domain} cannot change while locked: {reason}")
