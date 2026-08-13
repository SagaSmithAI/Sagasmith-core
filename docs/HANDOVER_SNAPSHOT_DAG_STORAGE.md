# Handover: Snapshot DAG Storage Strategy

## Question

Should campaign snapshots stop storing a complete materialized payload and become
a fully incremental DAG in which every node stores only its delta from its
parent?

## Current design

`CampaignSnapshot.parent_id` already forms a DAG lineage and each branch points
at a base and head snapshot. A snapshot currently stores a complete, checksummed
payload containing campaign state, rule and add-on locks, characters, scene
progress, active modules, events, continuity facts, actor knowledge, and the
undo/redo cursor.

The storage model is already partly incremental outside that JSON payload:

- continuity facts and actor knowledge use immutable revisions plus branch heads;
- snapshots index the exact fact, event, and actor-knowledge set through binding
  tables;
- events and state revisions remain append-only records rather than being
  rewritten for every branch.

The payload therefore duplicates some indexed continuity data, but it also gives
restore, checkout, integrity verification, and export a self-contained state
document with bounded read cost.

## Recommendation

Do **not** move directly to a fully incremental DAG. First measure real snapshot
size, creation latency, and restore latency. If payload duplication is material,
move to a hybrid design: periodic full checkpoints plus typed deltas between
checkpoints.

A fully incremental chain makes every descendant depend on all ancestors. One
missing, corrupt, or no-longer-decodable ancestor can make an entire subtree
unrestorable. It also makes schema migration, portable export, deletion and
garbage collection, and bounded restart/resume latency substantially harder.
Those costs currently outweigh an unmeasured storage saving.

## Proposed experiment

1. Add read-only telemetry for serialized payload bytes, duplicated continuity
   bytes, snapshot depth, creation time, and restore/materialization time.
2. Record the metrics in long-play and branch-heavy regressions without changing
   the persistence contract.
3. Define a typed state document and typed delta contract. Do not use an
   unvalidated generic JSON Patch as the authority for domain state.
4. Prototype materialization behind an internal repository boundary with a full
   checkpoint every configurable number of nodes or accumulated delta bytes.
5. Keep the current schema readable during the experiment. Only migrate after
   restore, checkout, branch, export/import, integrity, restart/resume, and
   corruption tests pass against both formats.

## Required invariants for any redesign

- A snapshot's checksum must authenticate its own record and its parent identity.
- Materialization must have a bounded maximum chain length and deterministic
  canonical output.
- Restore and checkout must remain atomic and must continue advancing the live
  campaign revision rather than restoring an old concurrency token.
- Branch creation, snapshot deletion, and garbage collection must use explicit
  reachability rules; no reachable ancestor may be removed.
- Export must be self-contained, either by including all required ancestors or by
  materializing a checkpoint at the export boundary.
- Schema upgrades must not require replaying deltas whose semantics are no longer
  available.
- Corruption tests must cover a bad leaf, bad ancestor, missing ancestor, cycle,
  cross-campaign parent, and interrupted checkpoint compaction.
- Recovery tests must cover idempotent retry, CAS conflict, restart/resume,
  snapshot/branch restore, and undo/redo cursor reconstruction.

## Decision gate

Adopt hybrid checkpoint-plus-delta storage only if representative long-play data
shows a meaningful storage or write-amplification problem and the prototype keeps
restore latency within the existing regression budget. Fully incremental storage
should remain rejected unless there is a concrete requirement that checkpoints
cannot satisfy.
