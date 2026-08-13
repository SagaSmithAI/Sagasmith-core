# Migration: system-neutral Core boundaries

This branch deliberately removes game-specific behavior from Core. System
packages and MCP hosts should migrate at the same release boundary; Core does
not provide aliases for the superseded protocols.

## Module scenes

- Translate system roles to `restricted`, `group`, or `public` before module
  ingestion. For example, a CoC profile normally maps `keeper` to `restricted`
  and `party` to `group`.
- Read system-specific scene values from `profile_data`. Fields such as clues,
  checks, sanity/stress, transitions, and node identifiers are no longer a
  fixed top-level Core superset.
- Module Pack validation rejects profile-owned values left at the top level of
  scene `metadata`; producers must move them under `profile_data` before import.
- Generic Markdown block quotes use the neutral chunk type `blockquote` rather
  than the presentation-specific `read_aloud` label.
- Module Packs require exact Agent finalization, complete Scene Atlas evidence,
  and normalized module-review records.

## Runtime locks

`RuleProfileService` no longer accepts `active_combat_option_keys`, and Core no
longer reads `campaign.state.combat.active`. The authoritative MCP declares a
generic entry in `campaign.state.mutation_locks` and names the affected domains:

```json
{
  "mutation_locks": [
    {
      "id": "encounter:current",
      "domains": ["rule_profile", "rule_pack_activation", "addon_activation"],
      "reason": "active encounter"
    }
  ]
}
```

The system MCP decides when that lock begins and ends. Core only enforces the
declared domains.

## Retrieval

Pass system vocabulary through `query_hints`. Core's default query expansion is
language- and game-neutral.

## Concurrency and recovery

- Campaign, character, import-job, fact-head, and actor-knowledge writes use
  database conditional updates. Recovery calls should pass both expected
  campaign revision and expected branch id.
- Branch creation advances the campaign revision, including creation without
  checkout. Refresh the campaign binding before a subsequent write.
- A continuity commit that combines reversible state documents with events,
  facts, knowledge, progress, or receipts is marked non-reversible. Use a
  snapshot or branch recovery instead of document-only undo/redo.
