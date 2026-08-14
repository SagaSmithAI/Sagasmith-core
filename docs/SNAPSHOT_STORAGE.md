# Snapshot storage

SagaSmith stores every campaign snapshot as one self-contained schema-v8 record.
The record contains a canonical state-document checksum, a bounded `zlib-1`
payload, its declared uncompressed size, and a checksum over both record
metadata and compressed bytes.

Capture writes a complete immutable state document. Restore, checkout,
integrity verification, and export all pass through the same bounded decode
boundary. No operation replays ancestor payloads, and deleting a snapshot does
not make another snapshot undecodable.

Integrity verification also checks that every activated module revision is
still available in the snapshot's campaign, independently of whether the
snapshot has addon locks. Restore repeats this authority check before changing
module activation state.

The current database contract is singular: `campaign_snapshots` must match the
schema-v8 model and every stored row must use schema version 8 and codec
`zlib-1`. Core does not provide alternate decoders, compatibility aliases, or
format conversion paths. A database that does not satisfy the current contract
must not be opened by the runtime.

Before replacing a database or runtime build, stop all writers and take a
consistent database backup. Rollback restores the database and runtime as one
matched unit; the current storage schema is not downgradable.
