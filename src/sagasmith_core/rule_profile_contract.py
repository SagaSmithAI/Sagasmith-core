"""Shared field contracts for campaign rule profiles and snapshot envelopes."""

from __future__ import annotations

RULE_PROFILE_FIELDS = frozenset({"edition", "locale", "publications", "options"})
SNAPSHOT_RULE_PROFILE_FIELDS = RULE_PROFILE_FIELDS | {"system_id"}
# Rule profiles are the sole owner of these concepts. Campaign settings must
# never carry shadow copies, regardless of whether the profile was created or
# restored from a snapshot.
RULE_PROFILE_OWNED_SETTING_FIELDS = frozenset({"edition", "locale"})
