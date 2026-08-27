# Database end-to-end audit and performance plan

Audit date: 2026-08-14

## Convergence addendum — 2026-08-15

The authorization and actor-lifecycle follow-up is now complete on the single
current runtime protocol:

- `AccessService.authorization_fingerprint` hashes campaign membership and the
  principal's complete actor-grant authority, so Host context epochs change on
  role, control, or private-view changes.
- `ActorLifecycleService.create` atomically creates a campaign actor, optional
  library-template lineage, initial grants, optional campaign state, one
  idempotency receipt, and one reversible mutation group. Undo removes the actor
  and its grants after checking external references; redo restores the same id
  and document.
- Snapshot schema 9 captures actor grants with campaign actors. Restore and
  branch checkout replace both together; schema 8 is not read through an alias
  or compatibility branch.

Public tests cover create/template retry, lifecycle undo/redo, snapshot/branch
restore, and grant integrity. The retained CoC private databases were exercised
only through isolated temporary copies at Alembic head `20260815_33`; both copies
passed SQLite `quick_check` and foreign-key checks after restart and current
runtime playthroughs. The v32 corpus counts and performance measurements below
remain the 2026-08-14 audit baseline and were not relabeled as a new full-corpus
scan.

## Technical summary

The audited v32 databases are structurally healthy and the public persistence
paths remain functional across Core, D&D, CoC, and Narrative MCP. All 60
accessible retained databases were upgraded from v29 to v32, compacted, and
passed SQLite `quick_check`, foreign-key checks, compressed-document checksum
checks, cross-table authority checks, and all 268 public snapshot integrity
checks.

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
compressed across the retained corpus. The actual bottlenecks have instead been
removed: StateRevision now references deduplicated compressed documents,
RulePackVersion references a compressed immutable payload, ImportJob keeps its
large review document compressed, and SQLite uses WAL with an explicit busy
timeout.

## Scope and evidence

The audit covered the following repositories at the recorded baseline. The first three remain
current sources; the standalone MCP repositories are archived historical evidence only. They are
not release inputs or compatibility fallbacks, and current authority lives in the corresponding
vertical domain repository:

- `sagasmith-core`
- `sagasmith-dnd`
- `sagasmith-coc`
- `SagaSmith-dnd-mcp` (archived; current authority: `sagasmith-dnd`)
- `SagaSmith-coc-mcp` (archived; current authority: `sagasmith-coc`)
- `SagaSmith-narrative-mcp` (archived; current authority: `sagasmith-narrative`)

The retained database cohort contains 23 `ttrpgbase.db` files and 37
`narrative.db` files. Before the storage work they totaled 1,753.281 MiB at
Alembic head `20260814_29`. All 60 now report the single supported head
`20260815_32`; compaction reduced their physical total to 1,025.250 MiB. A
further 157 historical temporary directories are unreadable because of Windows
ACLs and remain excluded rather than assumed healthy.

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
| State revisions | 10,974 | branch/mutation-group and document references valid |
| Snapshots | 268 | all decode and pass public `SnapshotService.verify` |
| Idempotency records | 5,995 | current schema and campaign references valid |
| Import jobs | 55 | current revisioned job schema |

The complete Ruff and test suites passed in all six repositories after the v32
change. Narrative MCP's one hard-coded v29 test assertion was advanced to the
current single head; no compatibility path was added. Focused
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
| Undo/redo | MutationGroup plus StateRevision | operation-level append; before/after are hashes of deduplicated compressed documents |
| Snapshot DAG | full canonical payload compressed with `zlib-1` | parent is lineage only; restore never replays ancestors |
| Rule Pack version | RulePackVersion identity/status plus immutable payload hash | full compressed version document, not byte delta |
| Import review | ImportJob hot state/revision plus compressed mutable document | whole document replaced under compare-and-swap, not a delta chain |
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
small fraction of the 1,753.281 MiB pre-migration retained database footprint.

An Avernus v29 temporary-copy profile measured its largest current snapshot at
238,738 uncompressed bytes and 33,892 compressed bytes:

| Phase | Measured time |
| --- | ---: |
| zlib decode | 0.306 ms median |
| SHA-256 payload checksum | 0.103 ms median |
| JSON decode | 1.091 ms median |
| complete integrity verification before P1 | 18.591 ms, 57 SQL statements |
| complete integrity verification after P1 | 14.571 ms, 13 SQL statements |
| live-state capture | 10.080 ms, 15 SQL statements |
| payload apply followed by rollback | 17.963 ms, 15 SQL statements |

The public checkout measurement was correctly blocked because the retained
active branch had unsaved changes. The historical 4.49-4.59 second checkout
record belongs to the deleted extreme stress database and has no phase profile;
it must not be attributed to JSON decoding.

### Snapshot integrity no longer has an N+1 query shape

Integrity verification now decodes a stored payload once and batch-loads
lineage, memory/revision, knowledge/revision, event, and participant records.
The Avernus sample fell from 57 to 13 SQL statements. A 1,000-event regression
fixture enforces a maximum of 25 statements, so ledger growth no longer makes
the integrity query count linear.

### State revision write amplification is removed

The 10,974 StateRevision rows contain 21,948 before/after JSON values totaling
332.150 MiB. Of those revisions, 5,965 store identical before and after
documents. The pre-migration estimates were:

| Storage form | Estimated size |
| --- | ---: |
| current JSON before/after values | 332.150 MiB |
| independent zlib per value | 53.510 MiB |
| content-addressed unique compressed documents | 26.307 MiB |

v30 implements the content-addressed design without a delta chain. Across the
60 upgraded databases, 5,342 unique per-database documents represent 147.266
MiB of canonical JSON in 28.218 MiB of compressed bytes. Equal before/after
states share hashes, branch clones share references, and undo/redo decodes the
exact referenced document with bounded size and checksum verification.

Narrative shows the same effect most clearly: a representative 73,961-byte
Campaign state at revision 126 produces a 14.668 MiB `state_revisions` table.

### Immutable rule Pack and import JSON are compressed

RulePackVersion JSON fields total 410.175 MiB in the retained corpus, but only
34 distinct field values exist. Independent zlib would reduce them to 29.687
MiB; a global content-addressed estimate is 2.234 MiB. This duplication was more
material than snapshot storage. v31 now combines the five immutable
RulePackVersion JSON fields into one content-addressed compressed payload. The
upgraded corpus stores 392.305 MiB of canonical payload in 28.982 MiB compressed
across 110 per-database documents. Version identity, status, checksum, and
campaign activation remain hot relational fields.

Rule chunks, sections, and FTS indexes are also repeated per MCP home. In the
largest current Waterdeep database, rule Pack versions use 19.418 MiB and rule
chunks/sections/FTS use another 41.227 MiB. These rows are valid and make local
search self-contained, but repeated test/runtime homes multiply the cost.

### Idempotency and import review bodies are bounded and compressed

Large idempotency responses already use a checksummed bounded `zlib` envelope
above 64 KiB while preserving exact replay and request-hash conflicts. v32 keeps
ImportJob identity, lifecycle state, revision, source bindings, parser metadata,
and errors as hot columns, while its five large review fields form one bounded
compressed document updated by the existing compare-and-swap. The upgraded
corpus stores 10.645 MiB of canonical import documents in 1.755 MiB compressed.

### SQLite now uses measured WAL concurrency

Three Avernus temporary-copy runs compared rollback journal and WAL with a
5-second busy timeout under two concurrent readers, campaign writes, snapshots,
online backup, restart, and integrity checks. WAL reduced update p50 from
4.9-5.3 ms to 2.4-3.1 ms, snapshot p50 from 41-45 ms to 15-16 ms, and total
elapsed time by 19%-34%. Neither mode produced lock errors; every backup,
restart, and `quick_check` passed. SQLite connections therefore now set WAL,
`foreign_keys=ON`, and `busy_timeout=5000`; `synchronous` remains the SQLite
durability default.

## Completed implementation order and acceptance

### P0: repaired write boundaries — complete

The module-integrity, package-asset, atomic Pack-import, and Narrative revision
fixes remain covered by failure injection and public-facade tests. The runtime
has one current protocol and no v7/v8 snapshot aliases or dual readers.

### P1: bounded snapshot verification — complete

Every public path decodes once and reuses the verified object. Integrity queries
are batched by collection type. The 1,000-event query-count gate and the complete
restart, restore, checkout, undo/redo, and branch suites pass. The retained
Avernus sample verifies in 14.571 ms with 13 statements. A historical
4.49-4.59-second checkout cannot be used as an acceptance baseline because its
stress database was deleted and no phase profile exists.

### P2: compressed StateDocument references — complete

Migration v30 backfills hashes, validates canonical bytes, replaces the two JSON
columns with foreign keys, and drops the retired columns. Runtime reads only the
new protocol. The retained compressed corpus is 28.218 MiB, below the 35 MiB
gate. Deduplication, corruption failure, branch sharing, undo/redo, fork, and
snapshot restore tests pass.

### P3: compressed Pack/import/idempotency payloads — complete

Migration v31 replaces five RulePackVersion JSON columns with an immutable
payload reference and removes unreferenced payloads when mutable drafts are
replaced or deleted. Migration v32 replaces five ImportJob JSON columns with one
compressed compare-and-swap document. Existing large-response idempotency
compression was retained rather than duplicated. Exact replay, corruption,
migration, Pack lock, archive, restart, and downstream MCP tests pass. The Rule
Pack compressed corpus is 28.982 MiB, below the 40 MiB gate; ImportJob is 1.755
MiB compressed.

### P4: WAL after concurrency evidence — complete

The measured WAL result justified the change. Every SQLite connection now uses
WAL and a 5-second busy timeout. The benchmark covered concurrent reads, writes,
snapshots, online backup, restart, and integrity. No fallback journal protocol
or configuration flag was added.

### P5: authority split — rejected for now

Snapshots remain self-contained compressed nodes. Facts and actor knowledge
remain true revision chains. StateRevision uses immutable document references,
not deltas. Rule Pack payloads deduplicate inside one database, but Core does not
gain a cross-database global catalog: there is still no demonstrated independent
runtime consumer with a defined atomic backup/export boundary. Narrative Pack
documents stay campaign-local because retained hot documents and settlement
profiles do not cross the 1 MiB / 100 ms escalation threshold. This is an
evidence-based boundary decision, not a permanent compatibility commitment.

## Migration, compatibility, and rollback boundary

- v30, v31, and v32 are one-way migrations. Their downgrade functions fail
  explicitly; rollback restores the matching pre-migration database and runtime.
- All 60 accessible v29 databases were backed up consistently, upgraded, and
  verified at v32. An all-upgraded-set rollback was available until verification
  completed. No database below v7 was present, so none was deleted.
- The migrations drop retired StateRevision, RulePackVersion, and ImportJob JSON
  columns. Runtime code contains no old-column read, alias, feature flag, or
  conversion endpoint.
- Migration temporarily raised physical size to 1,889.879 MiB because SQLite
  retained freed pages. Per-database backed-up `VACUUM` compacted the verified
  set to 1,025.250 MiB, reclaiming 864.629 MiB and leaving zero freelist pages.
- Future rollback must restore a consistent SQLite backup, including WAL
  checkpointing, rather than copying a live main file alone.

## Next performance work, in order

1. Add the 60-database v32 integrity scan and WAL backup/restart benchmark to
   release qualification so schema drift is detected before distribution.
2. Profile rule chunks, sections, and FTS duplication with at least two genuine
   runtime homes. Consider a shared immutable catalog only if atomic local
   export/backup and offline retrieval remain exact.
3. Add production telemetry for transaction p50/p95, busy-timeout failures,
   compressed-document decode failures, and snapshot statement counts. Do not
   add an orchestration layer; expose bounded counters at current facades.
4. Re-evaluate Narrative Pack extraction only if a real campaign crosses the
   1 MiB hot-document or 100 ms settlement threshold.

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
