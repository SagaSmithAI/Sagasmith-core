# SagaSmith Core

[中文](README.md) · [English](README-en.md) · [Platform overview](https://github.com/SagaSmithAI/.github/blob/main/profile/README.md)

**The system-neutral runtime for an AI-native TTRPG platform.** `sagasmith-core` gives rules systems, MCP servers, and clients persistent campaigns, actor knowledge, branching timelines, content ingestion, rule packs, and retrieval. It contains no D&D or Call of Cthulhu rules.

> World state should be verifiable, timelines should branch, and every actor should know only what they actually know.

## What it provides

- **Campaigns and characters** — system-neutral records, namespaced sheets, revisions, principals, and roles.
- **Branches and snapshots** — immutable snapshot DAGs, checkout, lineage, continuity, and integrity checks.
- **Actor knowledge** — facts scoped by actor, subject, branch, and visibility instead of one global summary.
- **Events and long-term memory** — event logs, stable fact identity, branch revisions, recaps, and continuity context.
- **Rule packs** — core/extension packages, profile locks, provenance, rule receipts, and mechanic IR.
- **Content ingestion** — resumable import jobs, content-addressed normalization/page caches, PDFium text extraction, selective OCR quality gates, and page-aware indexes.
- **Portable content** — one actor-card schema for PCs/NPCs/monsters; independently migratable module, preset, and source-bound rule packs; and authority-free release manifests.
- **Retrieval** — exact and lexical search, SQLite FTS5, plus optional ChromaDB and sentence-transformers.
- **System plugins** — D&D, CoC, and future systems register through the `sagasmith.systems` entry point.

## Where it sits

```mermaid
flowchart TB
    A[Agent / MCP Host] --> M[System MCP Server]
    M --> R[System Runtime<br/>D&D · CoC · custom]
    R --> C[SagaSmith Core]
    C --> D[(SQLite / PostgreSQL)]
    C --> F[FTS5]
    C -. optional .-> V[ChromaDB / embeddings]
```

Core does not decide GM style, MCP exposure, or system-specific rules. Skills own operating guidance, system runtimes own rules, MCP servers own the capability/storage boundary, and Core owns consistent data semantics.

## Shareable content formats

`sagasmith.portable` v1 is a system-neutral JSON envelope protected by
canonical JSON and SHA-256 checksums:

- `actor_card` is the shared PC/NPC/monster form; `actor_type` selects the role.
  Import creates a fresh local identity. Database ids, campaign ids, revisions,
  access grants, and ActorKnowledge are never exported.
- `module_pack` v2 is an independent `.sagasmith-module` ZIP archive. Its JSON
  descriptor carries classification, edition compatibility, party/level/
  advancement guidance, continuity policy, exact dependencies, normalized
  source, signed Scene Atlas, catalogs, narrative dossiers/relationships/
  endings, reviews, actor cards, component locks, and seven-dimensional
  readiness. Large assets live under content-addressed `blobs/sha256/` paths.
  Only `playable` or `complete` modules may activate. It excludes progress,
  world state, memory, random streams, branches, and Snapshots. The removed
  `sagasmith.module-pack.v1` schema is rejected; there is no compatibility reader.
- `preset_pack` distributes a reusable actor-card library, such as a game
  system's bundled standard creature cards.
- `rule_pack` carries the rule manifest, catalog artifacts, mechanic IR,
  provenance, and complete indexed sources/sections/chunks. Database UUIDs are
  replaced by stable `source_key`/`chunk_key` locators and rebound locally. Its
  `metadata.definition_checksum` pins rule semantics and dependencies without
  depending on local UUIDs or private/shareable distribution metadata.
- `release_manifest` composes exact rule, preset, and module package versions and
  full envelope checksums. It grants no install, activation, or access authority.

An `addon_pack` cannot embed or activate a module. Rules, presets, and modules
remain independent distributions connected by exact dependencies.

Core validates the generic envelope and rebuilds content through public service
paths. System plugins still validate sheets, editions, and rule dependencies;
applications/MCP servers still own authorization and import roots. See
`CharacterService.export_portable_card/import_portable_card`,
`ModuleService.export_portable_pack/import_portable_pack`, and
`ModuleService.bind_actor/list_actor_bindings`. Rule sources use
`RuleService.export_portable_source/import_portable_source`; `RulePackService`
still owns the separate draft, install, and campaign-activation lifecycle.

## Domain services

| Domain | Main services | Contract |
|---|---|---|
| Campaign | `CampaignService`, `AccessService` | system partitioning and principal/role boundaries |
| Character | `CharacterService`, `StateMutationService` | revisioned sheets, controlled mutation, actor-card import/export |
| Knowledge | `ActorKnowledgeService` | actor viewpoints and branch validity |
| Timeline | `SnapshotService`, `BranchService`, `ContinuityService` | ancestry, checkout, and continuity context |
| Content | `ImportJobService`, `ModuleService`, `PdfDocumentConverter` | resumable imports, provenance, structure, portable module/preset packs |
| Rules | `RuleService`, `RulePackService`, `RuleProfileService`, `RuleReceiptService` | portable sources, exact dependencies, versioned packs, active context, settlement evidence |
| Retrieval | `RuleService`, `VectorStore` | graceful degradation; vectors never own truth |

## Install

Requires Python 3.11+:

```bash
pip install sagasmith-core
pip install "sagasmith-core[documents]"  # PDF
pip install "sagasmith-core[documents,ocr]"  # scanned/corrupt-text PDF recovery
pip install "sagasmith-core[vector]"     # ChromaDB
pip install "sagasmith-core[embedding]"  # sentence-transformers
pip install "sagasmith-core[all]"
```

```python
from sagasmith_core import CampaignService, Database, SystemRegistry

db = Database("sqlite:///sagasmith.db")
db.upgrade_schema()
systems = SystemRegistry.discover()
campaigns = CampaignService(db)
```

## Add a game system

Register a package through an entry point:

```toml
[project.entry-points."sagasmith.systems"]
my_system = "my_package.system:get_system"
```

The package supplies its profile, character schema, module parser, and rules engine. Keep Core tables system-neutral; use namespaced JSON or explicit extension tables for system-specific state.

## Integrity boundaries

- Snapshots, branches, and revisions are authoritative; vector hits are not.
- A snapshot is a self-contained full checkpoint; only its `recap` is a delta from the parent. Integrity covers the payload, DAG ancestry, and fact/event/actor-knowledge bindings.
- Objective facts use stable `fact_key` identities with branch-scoped revision heads and optimistic revision checks. Subjective actor knowledge remains a separate ledger.
- Prefer `ContinuityCommitService` at scene boundaries so the event, fact upserts, actor-knowledge changes, and optional snapshot commit as one transaction.
- Checkout never silently discards a dirty worktree; save a snapshot before switching branches.
- Writes should use expected revisions and idempotency keys so agent retries cannot duplicate effects.
- Player reads are limited to visible branches, scene scopes, and actor knowledge; GM authority requires an explicit principal/role.
- Portable packages are not saves or permission carriers. Imported actors receive fresh identities and must acquire subjective knowledge in the target campaign; imported rule packs are never installed or activated automatically.
- Parsed content retains provenance, pages, parser profile, and quality warnings; rich metadata is best effort.
- Document caches are checksum- and profile-bound. Corrupt cache entries are ignored, and
  parser-version changes can reuse verified PDF page extraction/OCR without accepting stale
  normalized structure.
- This is an Alpha project. Current migrations serve the current mainline schema and do not promise legacy database compatibility.

## Development

```bash
pip install -e ".[all,dev]"
pytest --cov
ruff check .
```

Further reading: [Architecture](docs/ARCHITECTURE.md) · [Quickstart](docs/QUICKSTART.md) · [Retrieval](docs/RETRIEVAL.md)

## License

Apache-2.0
