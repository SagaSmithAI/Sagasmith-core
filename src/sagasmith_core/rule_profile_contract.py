"""Shared field contracts for campaign rule profiles and snapshot envelopes."""

from __future__ import annotations

RULE_PROFILE_FIELDS = frozenset({"edition", "locale", "publications", "options"})
SNAPSHOT_RULE_PROFILE_FIELDS = RULE_PROFILE_FIELDS | {"system_id"}
LEGACY_RULE_PROFILE_SETTING_FIELDS = frozenset({"edition", "locale"})
