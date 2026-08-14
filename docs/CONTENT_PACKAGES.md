# Unified content package standard

`sagasmith.content-package` schema version 2 is SagaSmith's only public content
exchange boundary. Core rules, addons, modules, and presets use the same archive
and evidence model. Their `kind` changes storage and activation semantics;
it does not create a second transport format.

## Archive

A `.sagasmith-pack` is a deterministic ZIP containing:

```text
package.sagasmith.json
blobs/sha256/<lowercase-sha256>
```

The descriptor checksum is calculated from canonical JSON without its
`checksum` field. Every asset names one exact blob checksum and byte size. The
archive contains exactly the declared blobs, with no absolute path, parent
traversal, duplicate member, loose JSON card, or nested legacy envelope.

The descriptor identity is repeated in `manifest.id`, `manifest.version`, and
`manifest.system_id`; all three values exactly match the package. Versions are
immutable. Dependencies pin `kind`, `id`, `version`, and descriptor checksum.

## Sources and evidence

Each source owns one `normalized_document` asset. Sections and chunks store
offsets, heading/page metadata, and SHA-256 hashes; they never duplicate source
prose in the descriptor. A generated chunk key contains source identity,
section ordinal, chunk ordinal, and the hash of the exact text slice, so equal or
empty chunks remain distinct and reproducible.

Redistributable packages also embed each available original document as an
`original_document` asset. Synthetic preset sources are the only sources that
need no separate original file.

Files distributed beside a source but not normalized or cited are typed
auxiliary assets, such as `map` or `player_reference`. They retain a logical
corpus path and package relationship but are never mislabeled as indexed
evidence. A public catalog may expose explicitly redistributable browser assets
by content hash while keeping the authoritative descriptor and every blob in
the downloadable archive.

Every citation uses exactly:

```json
{
  "source_key": "...",
  "chunk_key": "...",
  "page": 42,
  "note": "..."
}
```

The chunk belongs to the named source. Import localizes stable identities to
database IDs; export restores the same stable identities. Trusted localization
may rebind a previously validated content-review fingerprint, but it may not
change reviewed semantics or silently discard evidence.

## Actor cards and images

All PCs, NPCs, and monsters are `sagasmith.actor-card.v3` records distinguished
by `actor_type`. A card contains the complete system-owned sheet, narrative
notes, provenance, bindings, and metadata. It may reference one `actor_image`
asset by `asset_key`; actor import validates that reference against the owning
package's asset index. Image bytes are package assets and are never copied into
a campaign actor instance or snapshot. A system MCP may project a managed
portrait reference into its runtime character notes while the archive remains
the byte authority.

Source-backed portrait extraction requires a statblock heading, exact candidate
pages from actor evidence, a low-text visual region, and the configured
confidence floor. All candidate evidence pages are evaluated and the strongest
accepted crop is recorded with page, crop, method, and confidence. A missing or
uncertain illustration remains an explicit audit gap; no image is invented.

When a preset actor is composed with a rule package, the addon inherits the
corresponding statblock artifact's page-level evidence if the preset card has no
page-bearing citation. This preserves standalone card semantics while making
source art and later rulings traceable in the composed package.

## Kind semantics

- `core_rules`: stores immutable rule definitions; activation remains a
  campaign/branch decision.
- `addon`: stores optional rule definitions and actor catalogs; conflicts and
  activation policy are explicit.
- `module`: carries normalized/original documents, Scene Atlas, module actors,
  maps/assets, narrative context, endings, sourced play profile, and Agent
  finalization; activation is DM-only and separate from import.
- `preset`: stores reusable actor cards in a library; it does not activate a
  rule or module by itself.

The descriptor carries content and evidence, not a publication matrix. Core
validates structure, hashes, references, and kind invariants. System plugins add
semantic validation; finalized modules also require an explicit
`metadata.agent_finalization` record. Draft editing remains outside the Pack.

For a module that record is exact and contains `confirmed: true`, a non-empty
`reviewer`, and a non-empty `note`. Every Scene Atlas entry must have a unique
stable key and chapter/scene ordinal, bounded source span, and resolvable source
references. Module `content_reviews` use one exact normalized schema and exactly
one evidence mode: either a PDF/image page or one or more source references.
Review schemas carried by other Pack kinds remain system-owned, while core still
checks every embedded source reference.

Module import validates every blob before opening the database transaction and
then persists the module, assets, reviews, runtime actor instances, actor
bindings, and idempotency receipt in one ambient transaction. A failure cannot
leave a partially imported database graph. Content-addressed archive or image
files written before a failed commit are safe orphans rather than database
authority; a separate reference-aware garbage collector may remove them after
a grace period.

## Required invariants

Builders, MCP facades, public catalogs, and tests enforce:

1. deterministic archive and descriptor bytes;
2. exact source offsets, hashes, and cross-references;
3. cross-instance import/re-export identity;
4. immutable versions and exact dependency locks;
5. actor schema/system validation and external image assets;
6. role-, campaign-, phase-, and revision-aware import/activation boundaries;
7. no public compatibility route for `sagasmith.portable`, loose actor JSON,
   release manifests, or `.sagasmith-module` archives.
