# General TTRPG Base Architecture

`sagasmith-core` is a new project. It does not preserve the schema or runtime
behavior of earlier SagaSmith repositories.

## Domain boundary

Core owns system-neutral concepts:

- campaign identity, settings, and mutable state;
- characters and extensible character sheets;
- rule sources, hierarchical sections, retrieval chunks, and embeddings;
- module sources, chapters, scenes, retrieval chunks, and scene progress scoped
  to a party, split group, or individual player;
- parser and system-plugin protocols;
- transactional database, migrations, and optional vector storage.
- an objective fact ledger with stable identities and branch-local revision heads;
- an actor-knowledge ledger for beliefs, rumors, false beliefs, and disclosure;
- atomic continuity commits spanning campaign/character documents, scene
  progress, rule receipts, an event, fact changes, actor knowledge, and an
  optional snapshot;
- database-enforced optimistic revisions and host-declared runtime mutation
  locks.

System packages own game semantics:

- dice and checks;
- combat and advancement rules;
- system-specific character-sheet validation;
- rule terminology and parser enrichments;
- agent tools, skills, identity, and presentation.

Shared content follows the same boundary. Core owns the checksum-protected
`sagasmith.content-package` v2 archive and `sagasmith.actor-card.v3` records.
`addon`, `module`, `preset`, and `core_rules` packages share one manifest,
source-document, actor, asset, and content-addressed blob layout. A system plugin
validates each sheet and exact rule/module dependency. A package is immutable
authoring/source state, not runtime campaign state: ActorKnowledge, progress,
events, branches, random position, and Snapshots stay in their authoritative
ledgers. Imports replay stable structure even if the receiving parser changes.
Legacy portable envelopes and `.sagasmith-module` archives are not public
compatibility formats. Addons cannot own module activation.

## Extension policy

All common records carry `system_id`. System-specific fields should first use a
namespaced JSON object:

```json
{
  "dnd": {"armor_class": 16, "level": 3}
}
```

A system may add uniquely named extension tables when relational constraints
are required, for example `dnd_spell_slots`. It must not redefine or shadow a
core table.

## Integration direction

```text
sagasmith-dnd ─┐
sagasmith-coc ├─> sagasmith-core
custom-system ─┘
```

Core has no Agent-platform adapter. Agent hosts use a system-specific MCP
server as the authority and the MCP server composes these Core services.

## Storage protocol

The current SQLite schema is v32. Hot identity, authority, status, revision,
and binding fields remain relational and indexable. Large canonical documents
use bounded checksummed `zlib-1` storage at their owning boundary:

- snapshots are self-contained full payloads; parent links express lineage but
  are never required for decode;
- StateRevision before/after values reference deduplicated immutable documents;
- RulePackVersion references one immutable content-addressed payload;
- ImportJob keeps one mutable compressed review document under its relational
  compare-and-swap revision;
- oversized idempotency responses use a compressed replay envelope.

These are not byte-delta chains. Corruption fails closed at decode, and delete
or restore does not require replaying an unbounded ancestor sequence. SQLite
connections use foreign keys, WAL, and a 5-second busy timeout. A live database
backup must checkpoint/include WAL state; schema rollback restores a matched
database and runtime because v30-v32 deliberately have no downgrade protocol.

## Continuity ownership

`CampaignMemory` stores objective world facts. Every new integration should
supply a campaign-stable `fact_key`, normally composed from a subject reference
and predicate. `MemoryRevision` stores lifecycle, disclosure, importance,
valid-time, and source-event evidence; `BranchFactHead` selects the revision
visible in one timeline.

`ActorKnowledge` stores what one live actor believes or remembers. It must not
be replaced by campaign facts or free-form character notes. Forgotten and
superseded heads are excluded from normal recall but remain available for audit.

At a scene boundary, `ContinuityCommitService` is the preferred write path. It
allocates the event sequence atomically and either commits every requested
ledger update and snapshot or rolls the whole unit back. A commit that spans
state documents and continuity side ledgers is intentionally not eligible for
document-only undo/redo; recover it through a snapshot or branch so the ledgers
cannot diverge.

Core does not know whether a lock represents combat, a chase, a negotiation, or
another system activity. An authoritative runtime may declare
`campaign.state.mutation_locks` entries with generic `domains` such as
`rule_profile`, `rule_pack_activation`, or `addon_activation`. The service that
owns a domain enforces the matching lock at its write boundary.

Scene progress uses a stable `scope_id`: `party`, `group:<id>`, or
`player:<character-id>`. A scoped current-scene read may inherit `party` until
that scope records its own scene. Writes and current-scene replacement affect
only the selected scope.

## Scene metadata ownership

A parsed scene carries stable column-backed fields and profile-owned metadata.
Public scene structures keep system values under `profile_data`; callers must
not assume one game's vocabulary exists for another game.

| Field | Source | Always present? |
|-------|--------|----------------|
| `scene_type` | `ModuleScene.scene_type` column | Yes |
| `headings` | `ModuleScene.headings` column | Yes |
| `scene_level`, `line_count`, `subsections`, `tags` | Generic profile enrichment | If profile does |
| `visibility` | Canonical core scope: `restricted`, `group`, or `public` | Yes |
| `spatial` | Reviewed generic spatial evidence | Yes, possibly empty |
| `profile_data` | All other profile-owned values | Yes, possibly empty |

System packages choose which fields their profile writes and translate their
own visibility vocabulary to the canonical scopes before ingestion. A profile
that omits an enrichment is not an error; consumers inspect `profile_data`
instead of relying on a fixed cross-system superset.

## Operational evidence

See [DATABASE_END_TO_END_AUDIT.md](DATABASE_END_TO_END_AUDIT.md) for the current
cross-repository integrity evidence, incremental-storage boundaries, measured
bottlenecks, migration rules, and performance acceptance gates.
