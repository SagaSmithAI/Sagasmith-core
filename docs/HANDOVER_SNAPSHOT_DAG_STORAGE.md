# Handover: Snapshot DAG Storage Decision

## Decision

Do **not** replace campaign snapshots with an unbounded fully incremental DAG.
The measured data supports this order instead:

1. use a self-contained compressed snapshot record;
2. after measuring that format in current-schema full playthroughs, consider
   immutable bindings specifically for `characters` and `revision_cursor`;
3. consider checkpoint plus typed delta storage only if those two changes still
   leave a demonstrated snapshot-specific capacity or write-latency problem;
4. keep a fully incremental chain rejected.

The public state document and restore semantics remain authoritative; compression
is an internal storage concern and does not add an ancestor replay dependency.

## Implementation status (2026-08-14)

Phase 1 is implemented by snapshot schema v8 and Alembic revision
`20260814_29`:

- the database stores `compressed_payload`, `payload_codec`,
  `uncompressed_size`, the canonical document `checksum`, and a
  `record_checksum`; the former JSON `payload` column is removed;
- `zlib-1` is the single current codec, with a 64 MiB declared and enforced
  uncompressed-size limit;
- the record checksum covers the compressed bytes plus schema, snapshot,
  campaign, branch, parent, slot, codec, size, and document checksum identities;
- every service obtains full state through one bounded `_materialize` boundary;
- public `get` and `export` still return the complete JSON document; the removed
  `storage_mode` discriminator no longer advertises a nonexistent dual protocol;
- the one-time migration accepts complete checksum-valid schema-v7 payloads and
  rejects v3–v6 instead of adding a runtime compatibility path;
- downgrade is deliberately unavailable; rollback restores the pre-migration
  database and matching runtime.

On a temporary copy of Avernus v29, all eight real v7 snapshots migrated and
verified as v8. Compressed payload bytes were 220,603 versus 1,521,530 raw
(85.5% smaller), and the SQLite snapshot table allocation fell from 1,544,192
to 233,472 bytes. Migration took 0.124 s, a subsequent real restore took 0.102
s, and export returned a valid full JSON document. These figures are one-host
validation evidence, not a cross-platform latency SLO.

Phase 2 targeted bindings and Phase 3 checkpoint-plus-delta remain gated future
work. A fully incremental DAG remains rejected.

## Audit scope and method

The audit used `sagasmith-core` at `bfe5ce2` (the rewritten equivalent of the
handover's `13490d9`; the only tree difference is a trailing blank line in this
document). All retained regression databases were opened through SQLite
`mode=ro&immutable=1`. The only write experiment used a temporary 967 MiB copy
under the core worktree; it was removed after measurement.

The Avernus stress snapshots are schema version 3, while current core accepts
only `SnapshotService.SCHEMA_VERSION == 7`. They are valid historical size and
workload evidence, but current core cannot directly restore or checkout them.
An attempted envelope-only adaptation also failed current integrity validation
because v3 memory and event documents predate required v7 fields. This is useful
migration evidence: changing a version number and adding default fields is not a
valid conversion.

For a current-code timing sample, the temporary copy was reset and current
`_capture` created a genuine 900,943-byte v7 snapshot. A sibling branch was
created from that snapshot and checked out through the public core service. This
sample isolates the current implementation but is not a replacement for a
current-schema long-play benchmark.

## Reproduced storage evidence

Depth below is reported as both edge depth and lineage node count. The original
notes called a chain of 79 nodes “depth 79”; graph code normally calls it 78
edges.

| Sample | DB MiB | Snapshots | Max lineage | Payload MiB | Payload / DB | Per-node zlib MiB |
|---|---:|---:|---:|---:|---:|---:|
| D&D Avernus stress | 967.074 | 93 | 78 edges / 79 nodes | 59.421 | 6.14% | 9.849 |
| D&D snapshot audit 1 | 152.020 | 15 | 8 edges / 9 nodes | 3.154 | 2.08% | 0.387 |
| D&D snapshot audit 2 | 152.227 | 15 | 8 edges / 9 nodes | 3.367 | 2.21% | 0.409 |
| Avernus clean v28 | 81.535 | 6 | 3 edges / 4 nodes | 1.009 | 1.24% | 0.136 |
| Avernus clean v29 | 88.762 | 8 | 3 edges / 4 nodes | 1.451 | 1.63% | 0.213 |
| LMOP clean v3 | 70.820 | 6 | 3 edges / 4 nodes | 1.147 | 1.62% | 0.148 |
| SKT clean v1 | 83.191 | 9 | 3 edges / 4 nodes | 1.250 | 1.50% | 0.199 |
| Tomb clean v1 | 80.480 | 14 | 6 edges / 7 nodes | 1.071 | 1.33% | 0.205 |
| Tyranny clean v1 | 74.824 | 4 | 2 edges / 3 nodes | 0.289 | 0.39% | 0.057 |
| Waterdeep clean v2 | 80.531 | 6 | 3 edges / 4 nodes | 0.678 | 0.84% | 0.095 |
| CoC private main | 56.754 | 13 | 0 edges / 1 node | 0.150 | 0.27% | 0.047 |

The CoC main sample has 13 independent roots, not a 13-node chain. Other CoC
private databases contain zero or two snapshots. The four Narrative samples
contain zero or one snapshot each; the two one-snapshot payloads are 0.029 MiB
and 0.040 MiB. Narrative evidence is therefore insufficient for a storage
architecture decision.

The Avernus v29 run's `summary.json` says `complete: true` with no campaign gaps,
and its matrix includes `process_restart`, `snapshot_restore`,
`branch_checkout`, and `undo_redo`. It proves those public workflows for that
run; it does not make its short four-node lineage a stress test.

### Extreme payload composition

The stress payload is 59.421 MiB total, averages 654.3 KiB, and reaches 910.2
KiB. Compact encoding of top-level values attributes it as follows:

| Field | Raw MiB | Per-field zlib MiB |
|---|---:|---:|
| `characters` | 32.194 | 3.798 |
| `revision_cursor` | 13.226 | 3.965 |
| `campaign` | 3.575 | 0.490 |
| `actor_knowledge` | 2.781 | 0.506 |
| `events` | 2.187 | 0.692 |
| `scene_progress` | 0.728 | 0.241 |
| `memories` | 0.372 | 0.156 |

`characters` plus `revision_cursor` are 76.4% of raw component bytes and remain
the largest compressed components. That justifies targeted follow-up, but only
after the simpler compression result is measured in the real storage engine.

The independently reproduced whole-record zlib estimate is 9.849 MiB, an 83.4%
payload reduction. On this machine, in-memory zlib level-default work across all
93 nodes took 736 ms to compress (7.92 ms average, 11.76 ms maximum) and 86 ms to
decompress (0.92 ms average, 1.44 ms maximum). These are codec microbenchmarks,
not transaction measurements.

The earlier 17.349 MiB all-delta and 21.722 MiB ten-level-checkpoint estimates
remain directional only. There is no persisted typed-delta protocol or
measurement artifact to reproduce. A separate keyed-list estimator produced
different totals depending on whether list order and encoding overhead were
included. No delta percentage should be used as an acceptance claim until the
typed contract exists and can round-trip every state document.

### The 967 MiB database is not primarily a snapshot problem

SQLite `dbstat` attributes the stress database's leading allocations as:

| Object | Rows | Allocated MiB |
|---|---:|---:|
| `state_revisions` | 17,805 | 664.129 |
| `audit_logs` | 2,919 | 111.789 |
| `campaign_snapshots` | 93 | 59.609 |
| `rule_pack_versions` | — | 34.898 |
| `idempotency_records` | — | 22.113 |

`StateRevision.before` and `.after` alone contain 321.534 MiB and 322.581 MiB.
Compressing snapshots to the offline zlib estimate would save about 49.6 MiB,
or 5.1% of this database, before SQLite file-reclamation effects. It would be a
worthwhile local improvement, but it would not solve the dominant database
growth. A migration must rebuild the table or use an equivalent safe compaction
step if physical file reduction is an acceptance criterion; shorter updated
values do not by themselves guarantee a smaller SQLite file.

## Reproduced latency evidence

The retained benchmark records two historical checkouts at 4.590 s and 4.493 s
for slots 80 and 81, whose payloads are 897,297 and 897,379 bytes. It has no
stage profile and ran against the older snapshot protocol, so it cannot identify
JSON as the bottleneck.

On the temporary current-code v7 sample:

| Stage | Time |
|---|---:|
| Raw SQLite fetch of the 900,943-byte JSON, warm average | 0.55 ms |
| `json.loads`, warm average | 3.04 ms |
| Current-branch integrity | 337.3 ms |
| Current `_capture` inside clean check | 53.7 ms |
| Whole clean check | 398.9 ms |
| Target integrity | 342.9 ms |
| `_apply` | 114.8 ms |
| Commit phase | 11.2 ms |
| End-to-end checkout | 877.4 ms |

The checkout issued 778 SQL statements. Two integrity passes walked the
79-node ancestry independently; 158 `campaign_snapshots` selects consumed about
60.5 ms. Ledger validation also performed per-record reads, including 106
campaign-event selects. `_apply` spent most measured SQL time on the
`state_revisions` cursor (about 119 ms across its selects and updates). Character
and scene delete/insert statements consumed less than 9 ms of database execution
time in this sample. Continuity facts, events, and actor knowledge were not
rewritten during checkout; branch and snapshot bindings select their visibility.

The result is decisive only at the category level: JSON decode is not the
current checkout bottleneck. Integrity query shape, ancestry checks, cursor
materialization, and live-state capture dominate. Compression should be judged
as a capacity/write-byte change and must not be sold as the checkout-latency fix.

After the synthetic sibling checkout, a return checkout was correctly excluded
from the timing set because the branch was considered dirty: branch creation had
cloned `StateRevision` row ids while the shared head payload retained source
cursor ids. This identity boundary must be resolved before extracting
`revision_cursor` into bindings; the failed return is not evidence for or
against a storage codec.

## Current authority and operation boundaries

### Model authority

- `CampaignSnapshot` owns one immutable state payload, payload checksum, schema
  version, parent pointer, originating branch, and recap. `parent_id` describes
  history but is not required to materialize the current full payload.
- `CampaignBranch` is a ref with `base_snapshot_id` and `head_snapshot_id`.
  `Campaign.active_branch_id` selects the checked-out worktree.
- `Character` and `SceneProgress` are mutable campaign-global materializations.
  Checkout deletes and recreates their current rows from the selected snapshot.
- `StateRevision` rows are branch-owned undo/redo records. Branch creation clones
  cursor rows and uses `branch_key` to refer to an immutable source payload.
- Memory and actor-knowledge content already use immutable revision rows plus
  branch heads. Snapshot fact, event, and actor-knowledge binding tables select
  exact visible revisions without copying or deleting those ledgers on restore.

These responsibilities belong in system-neutral core. No D&D, CoC, Narrative,
module, or Skill-specific rule is needed for the storage redesign.

### Capture and creation

`_capture` queries the live campaign, rule/add-on locks, all campaign characters,
scene progress, active module revisions, visible events and participants,
branch fact/knowledge heads, and the branch's state-revision cursor. Creation
then reads the parent's full payload for recap generation, hashes and writes the
new full payload, inserts continuity bindings, and advances the branch head in
one transaction.

This creates snapshot write amplification but also gives each node constant-hop
materialization. Restore amplifies writes further because it first saves the
source worktree, materializes a new branch from the target, and immediately
captures a restored child. Compression reduces blob bytes, not capture queries,
binding writes, recap comparison, or the much larger state/audit ledgers.

### Integrity

Current integrity accepts exactly schema v8 and crosses the single bounded
`_materialize` boundary. The record checksum authenticates the compressed bytes
and schema, snapshot, campaign, branch, parent, slot, codec, size, and canonical
document checksum identities. Integrity then validates required shapes and
installed module/rule/add-on references, walks parent ids for cycles and
cross-campaign ancestry, and compares full memory/event/knowledge/cursor
documents with their indexed rows. Ancestor payloads are not replayed or made
part of a leaf's decode dependency.

### Restore and checkout

Restore is non-destructive history: it checks optimistic concurrency, validates
the target, captures a pre-restore snapshot, creates a new branch, copies
continuity heads and revision cursor state, changes the active ref, applies the
target, and captures a child. The transaction rolls back all steps if
materialization fails. Live `Campaign.revision` remains monotonic rather than
reusing the captured concurrency token.

Checkout first proves the current branch equals its head (apart from the live
campaign revision), validates the target, changes the active ref, and applies
the target atomically. Snapshot, Branch, access, rule-pack, add-on, and
continuity services all use the same narrow `_materialize(snapshot)` boundary;
there are no direct legacy `snapshot.payload` reads or alternate decoders.

### Delete and export

Delete currently permits only a leaf that is not a branch base or head. Because
every payload is full, deletion does not remove state required by another node.
Delta storage would turn ancestry into a data dependency and require reachability
and garbage-collection rules in addition to the current history rules.

Export calls `get`, verifies the snapshot, and writes a full JSON document with
metadata, recap, payload, schema version, and validity. It exports state, not
the installed rule/module artifacts required to apply that state. Compression
remains internal and does not add a public storage-mode discriminator. A delta
format would have to materialize all ancestors or emit the whole dependency
closure.

## Separate problem statements

| Problem | Evidence | Decision consequence |
|---|---|---|
| Storage capacity | Ordinary D&D payload is 0.39–1.63% of DB; extreme is 6.14%. Snapshot compression saves about 5.1% of the extreme whole DB. | Improve locally with compression, but investigate state/audit growth separately. |
| Restore/checkout latency | JSON decode is about 3 ms in the profiled v7 sample; integrity and cursor work dominate. | Optimize integrity query shape and cursor handling independently of storage format. |
| Snapshot write amplification | Every create stores full characters and an accumulating cursor; restore creates two additional full snapshots. | Compression is the lowest-risk byte reduction; targeted bindings may later remove repeated fields. |
| Reliability and migration | Current v3 records cannot be envelope-upgraded to v7; full nodes localize corruption and simplify export/delete. | Require one explicit offline migration and retain bounded, ancestor-independent materialization. |

## Option comparison

| Option | Capacity / writes | Read latency | Reliability, delete, export, migration | Decision |
|---|---|---|---|---|
| Keep full JSON | No improvement | Constant-hop; current decode is cheap | Simplest and most isolated | Safe baseline, but leaves an easy 83% payload reduction unused |
| Self-contained compressed payload | Strong measured reduction in blob bytes | Still constant-hop; measured decompression is under 1.5 ms per stress node | Almost the same failure domain; export decodes one record; one bounded migration | **Implemented** |
| Immutable character and cursor bindings | Targets 76% of raw payload without ancestor chains | Bounded direct joins, but cursor identity/materialization must be fixed | More shared-row dependencies and GC rules; still ancestor-independent | **Conditional second phase** |
| Periodic checkpoint plus typed delta | Potential reduction between checkpoints | Bounded by checkpoint interval and accumulated delta bytes | Typed schema, compaction, reachability, corruption, and migration complexity | Defer unless earlier phases miss explicit gates |
| Fully incremental DAG | Maximum theoretical de-duplication | Unbounded replay and ancestor validation | One bad/missing/obsolete ancestor can lose a subtree; hardest delete, export, GC, and upgrade | **Reject** |

The unvalidated uncompressed checkpoint estimate is larger than the measured
compressed full payload. Even if compressed deltas improve that comparison,
complexity is not justified without a production requirement that a full
compressed node cannot satisfy.

## Executable follow-up design

### Phase 1: self-contained compressed record (implemented)

Keep one canonical typed state document identical to the current public payload.
At capture:

1. construct and validate the complete document;
2. canonicalize it once for the document checksum;
3. compress those bytes with one explicitly versioned codec;
4. store uncompressed length, document checksum, and a record checksum over
   storage version, campaign id, snapshot id, parent id, document checksum, and
   codec id;
5. insert bindings and move the branch head in the existing transaction.

At read, enforce a maximum uncompressed length before allocation, decompress one
record, verify length and both checksums, decode the document, and pass the same
typed value to integrity/apply/export. Do not add a generic codec registry; the
new schema has one current codec and one current protocol.

`zlib-1` is the selected codec. Any future replacement must cross another
explicit one-time schema cutover after comparing ratio, p95 capture and
materialization overhead, deterministic output, maintenance, and corruption
behavior. Do not add a registry, dual decoder, or codec negotiation path. The
document checksum remains over canonical uncompressed state so a future cutover
would not redefine state identity.

Independently of compression, batch integrity reads for ancestry, facts,
knowledge, events/participants, and cursor ids. This is a query-shape change,
not a reason to introduce deltas.

### Phase 2: targeted immutable bindings, only if gated in

Do not create a generic field-versioning framework. Two explicit contracts have
evidence:

- `CharacterRevision(character_id, revision_id, canonical_document, checksum)`
  plus `SnapshotCharacterBinding(snapshot_id, character_id, revision_id,
  ordinal)`. Create a revision only when the canonical character document
  changes. Checkout resolves one directly bound revision per character and
  rebuilds the mutable `Character` worktree. Actor-knowledge references keep the
  stable character id.
- `SnapshotRevisionCursorBinding(snapshot_id, source_revision_id, ordinal,
  applied, redoable)`. Bind the canonical source identity (`branch_key` where
  applicable), not a branch clone's transient row id. Branch creation clones
  branch-owned cursor rows from these bindings, and clean comparison normalizes
  back to the canonical source identity.

Both designs need explicit foreign-key/reachability rules, bulk loading, stable
ordering, checksum coverage, branch clone tests, and export materialization.
They must not depend on walking snapshot parents.

### Phase 3: checkpoint plus typed delta, only if still necessary

If a remaining requirement passes the gate below, define field-specific
operations such as campaign-field replacement, character revision binding set,
scene-progress upsert/remove, event/fact/knowledge binding set, and cursor
binding set. Do not use generic JSON Patch as persisted authority.

A checkpoint policy must bound both node count and accumulated delta bytes.
Compaction creates a new authenticated checkpoint; it never mutates released
nodes in place. Integrity validates every segment dependency. Export always
materializes one full current document. Delete and GC operate on explicit
reachability and cannot remove a reachable checkpoint or delta.

## Migration and compatibility boundary

Revision `20260814_29` is the only v7-to-v8 boundary. The current runtime has no
dual reads, aliases, fallback decoding, codec negotiation, or dual writes.

1. The operator stops writers and creates a consistent database backup before
   launching the new runtime.
2. The migration preflights every source row before its first schema mutation;
   only complete, checksum-valid schema-v7 documents are accepted.
3. It canonicalizes and compresses records in deterministic slot order, binds
   their immutable identities in the record checksum, and writes the v8 fields.
4. It makes the v8 envelope non-null and removes the old JSON `payload` column
   in the same Alembic revision.
5. Only the new runtime starts against the migrated database. Real-copy and
   public recovery tests verify materialization, export, restore, and checkout.

The schema-v3 regression databases are analytical fixtures, not valid inputs to
that v7 migration. If product requirements demand recovery of older released
snapshots, use a pinned historical runtime to verify and export/materialize them,
then cross an explicit reviewed conversion boundary into one current snapshot.
Do not reintroduce the removed schema-3/4/5/6 compatibility branches.

## Rollback

- A preflight mismatch occurs before schema mutation and leaves the source
  protocol selectable by its matching runtime.
- After cutover, rollback restores the byte-for-byte pre-migration database and
  the matching old runtime. Do not attempt lossy down-conversion or keep a
  shadow writer.
- Keep the backup and recorded preflight evidence until the full public recovery
  matrix passes and the operator explicitly closes the rollback window.
- If migration is interrupted, do not introduce a mixed-protocol recovery path
  or let either runtime guess the state; restore the pre-migration backup and
  rerun the single cutover.

## Acceptance gates

### Compressed full records

- Aggregate stored snapshot bytes are at most 25% of raw canonical bytes on the
  Avernus stress corpus and at least one current-schema full-playthrough corpus.
- Added p95 compression time is at most 20 ms per approximately 1 MiB snapshot;
  added p95 decompress-plus-decode time is at most 10 ms on the same host class.
- End-to-end current-schema create, restore, and checkout p95 does not regress by
  more than 10%; report stage timings rather than only a total.
- Decode reproduces byte-identical canonical state plus the required current
  export metadata for every migrated snapshot.
- Corruption tests cover truncated/compressed garbage, decompression-size limit,
  document checksum mismatch, parent substitution, wrong campaign, missing
  binding, and interrupted migration.
- Public regression covers Lobby -> Play -> Combat -> Play, Grid and Agent
  spatial modes, DM and player audiences, NPC settlement, chase/Combat
  exclusivity, idempotent retry, revision refresh, restart/resume,
  snapshot/branch restore, and undo/redo through real host behavior.

### Targeted bindings

Proceed only if compressed snapshots still exceed 10% of a representative
database **or** measured snapshot write bytes/latency violate an explicit
operational SLO. Require branch creation and immediate round-trip checkout to be
clean, direct-reference materialization with no parent walk, bulk query counts
bounded by type rather than item count, and the same corruption/export/recovery
matrix as compressed full records.

### Checkpoint plus typed delta

Proceed only if targeted bindings still miss an explicit SLO and a typed
prototype saves at least 30% beyond compressed self-contained records after
indexes, bindings, checkpoints, and GC metadata. Maximum replay is ten nodes or
an independently chosen byte cap, whichever comes first. p95 restore/checkout
must remain within 10% of compressed full records, and loss of one delta may
invalidate only its bounded checkpoint segment, not an unbounded subtree.

There is no acceptance gate for an unbounded fully incremental chain under the
current requirements. A new product requirement would need to show why bounded
checkpoints and targeted immutable bindings cannot satisfy it.
