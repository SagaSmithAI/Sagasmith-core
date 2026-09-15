"""Versioned lifecycle for trusted, system-owned extension tables.

Callbacks participate in the caller's transaction and must never commit it.
Real-user permissions must not be included in extension story state.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from sagasmith_core.integrity import canonical_json


class StateExtension(Protocol):
    id: str
    system_id: str
    schema_version: int

    def capture(self, session: Session, campaign_id: str) -> dict[str, Any]: ...
    def migrate(self, state: dict[str, Any], from_version: int) -> dict[str, Any]: ...
    def validate(self, state: dict[str, Any]) -> None: ...
    def clear(self, session: Session, campaign_id: str) -> None: ...
    def restore(self, session: Session, campaign_id: str, state: dict[str, Any]) -> None: ...


def _adapters(session, system_id):
    return {
        item.id: item
        for item in session.info.get("state_extensions", ())
        if item.system_id == system_id
    }


def capture_extensions(session, campaign) -> dict[str, Any]:
    result = {}
    for key, adapter in sorted(_adapters(session, campaign.system_id).items()):
        state = json.loads(canonical_json(adapter.capture(session, campaign.id)))
        adapter.validate(state)
        result[key] = {"schema_version": adapter.schema_version, "state": state}
    return result


def prepare_extension_restore(session, campaign, payload):
    adapters = _adapters(session, campaign.system_id)
    values = payload.get("extensions", {})
    if not isinstance(values, dict) or set(values) - adapters.keys():
        raise ValueError("snapshot requires unavailable state extensions")
    prepared = []
    for key, adapter in sorted(adapters.items()):
        envelope = values.get(key, {"schema_version": adapter.schema_version, "state": {}})
        version = envelope["schema_version"]
        if not isinstance(version, int) or version < 1 or version > adapter.schema_version:
            raise ValueError("unsupported extension state schema")
        state = json.loads(canonical_json(envelope["state"]))
        if version != adapter.schema_version:
            state = adapter.migrate(state, version)
        adapter.validate(state)
        prepared.append((adapter, state))
    return prepared
