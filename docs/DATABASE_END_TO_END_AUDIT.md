# Database end-to-end audit and performance plan

Audit date: 2026-08-14

## Technical summary

The current v29 databases are structurally healthy and the public persistence
paths remain functional across Core, D&D, CoC, and Narrative MCP. Sixty
accessible runtime databases passed SQLite `quick_check`, foreign-key checks,
cross-table authority checks, and all 268 public snapshot integrity checks.

Four write-boundary defects were found and fixed:

1. snapshot verification skipped module availability when `addon_lock` was
   empty;
2. actor cards with package-owned images could not cross the Core character
   import boundary;
3. D&D and CoC Pack imports could commit the module before actor materialization
   or binding failed;
4. Narrative actor creation returned and replayed `revision: null` even though
   the persisted actor revision was `1`.

The evidence does not support replacing self-contained compressed snapshots
with an unbounded incremental DAG. Current snapshots occupy only 3.541 MiB
compressed across the retained corpus. The dominant avoidable storage and write
costs are full before/after state revision documents and repeated immutable rule
Pack JSON.

## Scope and evidence

The audit covered these current repositories on `main`:

- `sagasmith-core`
- `sagasmith-dnd`
- `sagasmith-coc`
- `SagaSmith-dnd-mcp`
- `SagaSmith-coc-mcp`
- `SagaSmith-narrative-mcp`

The retained database cohort contains 23 `ttrpgbase.db` files and 37
`narrative.db` files, totaling 1,753.281 MiB. All 60 report Alembic head
`20260814_29`. A further 157 historical temporary directories are unreadable
because of Windows ACLs and are excluded rather than assumed healthy.

| Grain | Retained count | Checks |
| --- | ---: | --- |
| Campaigns | 112 | every active branch resolves inside its campaign |
| Branches | 205 | every snapshot head resolves inside its campaign |
| Characters | 518 | campaign/system/template relationships valid |
| Module revisions | 89 | campaign ownership and snapshot activation valid |
| Module chunks | 29,492 | relational and FTS migrations current |
| Memory identities / revisions | 95 / 119 | revision parents and branch heads valid |
| Actor knowledge identities / revisions | 188 / 188 | revision parents and actor ownership valid |
| Events | 633 | bindings and snapshot payloads agree |
| State revisions | 10,974 | branch and mutation-group references valid |
| Snapshots | 268 | all decode and pass public `SnapshotService.verify` |
| Idempotency records | 5,995 | current schema and campaign references valid |
| Import jobs | 55 | current revisioned job schema |

The complete Ruff and test suites passed in all six repositories. Focused
failure-injection tests additionally prove that a module Pack interruption
after actor binding leaves neither a module nor a character in the database.

## Current authority and incremental boundaries

| Area | Current authority | Incremental behavior |
| --- | --- | --- |
| Content package | deterministic schema-v2 archive and exact checksum lock | immutable full version, not byte delta |
| Module graph | relational module/chapter/scene/chunk revision | new full materialized revision; previous revision retained |
| Campaign | mutable Campaign row plus active Branch pointer | current state is materialized; history is separate |
| Portable actor | `sagasmith.actor-card.v3` inside its package | immutable package content |
| Library actor | Character with `campaign_id IS NULL` | mutable full row |
| Campaign actor | Character with campaign and optional `template_id` | mutable full row; operation history is incremental |
| Long-term memory | stable identity plus immutable MemoryRevision | true fact-level revision chain and branch head |
| Actor knowledge | stable identity plus immutable knowledge revision | true knowledge-level revision chain and branch head |
| Events | CampaignEvent ledger | append-only |
| Undo/redo | MutationGroup plus StateRevision | operation-level append, but full entity before/after documents |
| Snapshot DAG | full canonical payload compressed with `zlib-1` | parent is lineage only; restore never replays ancestors |
| Vector/FTS indexes | derived retrieval structures | rebuildable, not persistence authority |

Character templates and campaign instances are distinct but not mandatory
lineage. Of 456 retained campaign characters, 103 point to a library template
and 353 are valid direct campaign actors. The public Core `bind` operation can
also move a character between library and campaign scope. A future requirement
that every campaign actor must be an immutable-template copy would therefore be
a product-model change, not a migration repair.

Narrative MCP uses Core Campaign and Character rows, Core long-term memory, Core
events, Core snapshots, and Core idempotency. Its declarative Profile and Pack
documents are intentionally embedded in Campaign state rather than using the
public `.sagasmith-pack` exchange archive. That boundary is internally
consistent but does not currently provide cross-campaign Narrative Pack
exchange.

## Correctness findings and repairs

### Snapshot module verification now fails closed

The module availability query was nested under the addon loop. An empty addon
lock therefore bypassed the query. The query now runs independently, and a
focused test moves an activated module revision to another campaign and proves
that both verify and restore reject the snapshot.

### Package actor assets now cross the validation boundary

`CharacterService.import_content_actor` now accepts the owning package asset
index and passes it to actor-card validation. D&D and CoC module imports provide
that exact validated index. The D&D end-to-end test imports a module actor with
a real image blob and verifies the managed portrait reference on the resulting
runtime actor.

### Pack imports are one database transaction

D&D now wraps module, addon, core-rules, and preset import composition in one
ambient Core transaction. CoC wraps its complete import action in the same way.
Core services reuse the ambient session, so module graph, actors, bindings,
rules/addons, and replay receipt commit or roll back together. Package files are
content-addressed side effects and cannot create database authority by
themselves.

### Narrative actor creation returns the persisted revision

Narrative now flushes the Character insert before constructing the result and
idempotency response. First response, replay, and subsequent query therefore
agree on revision `1`.

## Performance findings

### Snapshots are no longer the main capacity problem

Across all 60 retained databases, 268 snapshots contain 20.823 MiB of canonical
JSON and 3.541 MiB of compressed payload. This is an 83.0% reduction and only a
small fraction of the 1,753.281 MiB retained database footprint.

An Avernus v29 temporary-copy profile measured its largest current snapshot at
238,738 uncompressed bytes and 33,892 compressed bytes:

| Phase | Measured time |
| --- | ---: |
| zlib decode | 0.306 ms median |
| SHA-256 payload checksum | 0.103 ms median |
| JSON decode | 1.091 ms median |
| complete integrity verification | 18.591 ms, 57 SQL statements |
| live-state capture | 10.080 ms, 15 SQL statements |
| payload apply followed by rollback | 17.963 ms, 15 SQL statements |

The public checkout measurement was correctly blocked because the retained
active branch had unsaved changes. The historical 4.49-4.59 second checkout
record belongs to the deleted extreme stress database and has no phase profile;
it must not be attributed to JSON decoding.

### Snapshot integrity has an N+1 query shape

Integrity verification currently loads each fact identity and revision, each
knowledge identity and revision, and each event plus its participants in
separate queries. The Avernus sample needed 42 event/event-participant queries
for 21 bound events. This is linear in ledger size and is the best explanation
for stress-case latency, even though current ordinary databases remain fast.

### State revision documents dominate mutable-history write amplification

The 10,974 StateRevision rows contain 21,948 before/after JSON values totaling
332.150 MiB. Of those revisions, 5,965 store identical before and after
documents. Offline estimates on current data are:

| Storage form | Estimated size |
| --- | ---: |
| current JSON before/after values | 332.150 MiB |
| independent zlib per value | 53.510 MiB |
| content-addressed unique compressed documents | 26.307 MiB |

Narrative shows the same effect most clearly: a representative 73,961-byte
Campaign state at revision 126 produces a 14.668 MiB `state_revisions` table.

### Immutable rule Pack JSON is repeated across runtime databases

RulePackVersion JSON fields total 410.175 MiB in the retained corpus, but only
34 distinct field values exist. Independent zlib would reduce them to 29.687
MiB; a global content-addressed estimate is 2.234 MiB. This duplication is more
material than snapshot storage.

Rule chunks, sections, and FTS indexes are also repeated per MCP home. In the
largest current Waterdeep database, rule Pack versions use 19.418 MiB and rule
chunks/sections/FTS use another 41.227 MiB. These rows are valid and make local
search self-contained, but repeated test/runtime homes multiply the cost.

### Idempotency and import review bodies need size budgets

Idempotency response JSON totals 44.941 MiB. The largest retained response is
1,303,097 bytes and duplicates import-job inspection/candidate data already
stored elsewhere. ImportJob JSON totals 11.739 MiB and compresses offline to an
estimated 1.827 MiB.

### SQLite settings favor durability over read/write concurrency

All 23 `ttrpgbase.db` files use rollback-journal `delete` mode and
`synchronous=FULL`. This is a valid durability default. It should not be changed
to WAL globally without multi-process, crash, backup, and restart evidence.
Freelist space is only 17.523 MiB across those databases, so automatic VACUUM is
not justified.

## Recommended implementation order

### P0: preserve the repaired write boundaries

- Keep failure-injection tests for module actor binding and idempotent retry.
- Add the 60-database integrity scan to release qualification.
- Keep one current schema and no v7/v8 dual readers or compatibility aliases.

### P1: remove avoidable snapshot latency without changing its format

1. Decode each snapshot payload once per public operation and pass the verified
   object through integrity, clean-check, and apply.
2. Batch-load memory/revision, knowledge/revision, event/participant, and lineage
   rows.
3. Add query-count and elapsed-phase metrics.

Acceptance gates:

- integrity SQL count is bounded by collection type rather than row count;
- a fixture with 1,000 events performs no more than 25 integrity statements;
- a 1 MiB snapshot verifies below 250 ms and branch checkout below 1 second on
  the reference Windows host;
- corruption, restart, restore, checkout, undo/redo, and branch tests remain
  unchanged.

### P2: replace StateRevision JSON duplication with immutable state documents

Add a content-addressed table containing canonical JSON checksum, codec,
uncompressed size, and compressed bytes. StateRevision points to before and
after document hashes; equal before/after states share one record. This is not a
delta chain, so undo/redo never depends on replaying ancestors.

Use a one-time migration only:

1. stop writers and take a consistent backup;
2. create and backfill compressed state documents;
3. verify every hash and replay undo/redo on sampled and stress databases;
4. switch all readers/writers to the new columns;
5. remove old JSON columns in the same supported migration line;
6. rollback only by restoring the matched pre-migration database and runtime.

Acceptance gates:

- current 332.150 MiB corpus falls below 35 MiB;
- identical before/after state writes one document reference;
- no unbounded reconstruction chain;
- undo/redo, fork, snapshot restore, and audit-log identity remain exact.

### P3: compress and deduplicate immutable Pack/import payloads

- Store RulePackVersion payloads once per checksum in a compressed immutable
  payload table; version rows retain identity/status/provenance bindings.
- Store terminal ImportJob review bodies as compressed immutable artifacts and
  keep only bounded summaries in hot rows.
- Replace oversized idempotency responses with compact stable result handles or
  compressed replay payloads; never weaken request-hash conflict detection.
- Add response-size limits and a reference-aware, grace-period garbage collector
  for orphaned content-addressed files.

Acceptance gates:

- retained RulePackVersion JSON falls below 40 MiB without changing exact locks;
- normal idempotency responses remain below 256 KiB unless they return an
  explicit artifact handle;
- replay responses remain byte-equivalent at the public facade;
- archive export works after process restart with no external network source.

### P4: benchmark SQLite concurrency before changing journal mode

On temporary copies, compare the current rollback journal with WAL plus an
explicit busy timeout under concurrent read, settlement, snapshot, restart, and
backup workloads. Adopt WAL only if it eliminates lock failures and improves
p95 latency without weakening crash recovery or portable backup behavior.

### P5: split authorities only when profiles justify it

- Keep self-contained compressed Snapshot DAG nodes.
- Keep fact/knowledge revisions incremental.
- Consider a shared immutable rule/content catalog only after measuring more
  than one real runtime consumer and defining backup/export behavior.
- Move Narrative finalized Packs or records out of Campaign state only if the
  hot document exceeds 1 MiB or p95 settlement exceeds 100 ms. Do not normalize
  them speculatively.

## Limitations and open product decisions

- No retained database contains ContentAddon rows. Addon behavior is covered by
  tests and temporary imports, not historical production-like data.
- CoC retains actor image bytes in the package/module asset graph but does not
  currently project a portrait reference into Character notes. Add a
  system-neutral CharacterAssetBinding only if CoC runtime portrait retrieval
  is a required product surface.
- Narrative Pack is a campaign-local declarative protocol, not the public
  content-package exchange archive. Cross-campaign exchange requires an
  explicit product decision and should replace, not coexist with, a second
  public import protocol.
- Mandatory character-template lineage would require removing direct actor
  creation and bind/unbind semantics; current data intentionally uses both.
- The 157 ACL-inaccessible historical directories remain outside the evidence
  boundary.
