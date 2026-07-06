# 🏗️ SagaSmith Core

[中文](README.md) | [English](README-en.md)

**System-neutral TTRPG application base** — database, documents, retrieval, and campaign runtime for `sagasmith-dnd`, `sagasmith-coc`, and other system plugins.

> *"One brick at a time, building adventures for a thousand tables."*

`sagasmith-core` contains no D&D or Call of Cthulhu rules. It provides a shared set of durable services that system plugins build on to register their rules and CLIs. You will normally install it indirectly through a system package.

---

## Ecosystem

| Repo | Role |
|------|------|
| 🏗️ **sagasmith-core** (this repo) | General engine — DB, docs, RAG, campaign runtime |
| 🎲 [SagaSmith-agent](https://github.com/dajiaohuang/SagaSmith-agent) | Complete AI DM runtime |
| ⚔️ [sagasmith-dnd](https://github.com/dajiaohuang/sagasmith-dnd) | D&D 5e system plugin |
| 🕯️ [sagasmith-coc](https://github.com/dajiaohuang/sagasmith-coc) | CoC 7e system plugin |
| 📦 [SagaSmith-dnd-skills](https://github.com/dajiaohuang/SagaSmith-dnd-skills) | D&D agent skill definitions |
| 📦 [SagaSmith-coc-skills](https://github.com/dajiaohuang/SagaSmith-coc-skills) | CoC agent skill definitions |
| ✍️ [SagaSmith-module-gen-skills](https://github.com/dajiaohuang/SagaSmith-module-gen-skills) | Standalone module generator |

---

## Features

- 🏛️ **Campaigns** — identity, settings, mutable state, `system_id` multi-tenancy
- 👤 **Characters** — extensible sheets via namespaced JSON
- 📜 **Rule Documents** — hierarchical sections, retrieval chunks, BGE-M3 dense embeddings
- 📖 **Module Management** — PDF/Markdown import, structure-aware chunking, scene indexes
- 🧩 **Scene Progress** — scoped to `party` / `group:<id>` / `player:<id>` with inheritance
- 💾 **Snapshot System** — immutable DAG save tree, audited revisions, branch-aware memory
- 🔍 **Retrieval** — ChromaDB HNSW vector search, lexical / FTS hybrid fallback
- 🗄️ **Database** — SQLAlchemy ORM, Alembic migrations, SQLite/PostgreSQL dual backend
- 🔌 **Plugin System** — `sagasmith.systems` entry points, pluggable profiles

---

## Architecture

```
System Plugins (sagasmith-dnd / sagasmith-coc)
        │
        ▼
┌─────────────────────────────────┐
│        sagasmith-core           │
│                                 │
│  Campaigns · Characters · Docs  │
│  Modules · Scenes · Chunks      │
│  Retrieval (Vector + Lexical)   │
│  Snapshots (DAG) · Memory       │
│  SQLAlchemy ORM · Alembic       │
│  System Plugin Protocol         │
└─────────────────────────────────┘
        │
        ├── SQLite / PostgreSQL
        └── ChromaDB (optional)
```

---

## Install

```bash
pip install sagasmith-core
```

System packages normally install it automatically. Core has no agent-platform dependency.

### Optional Extras

| Extra | Purpose |
|-------|---------|
| `vector` | ChromaDB vector store |
| `embedding` | sentence-transformers embeddings |
| `documents` | PDF parsing (pypdf) |
| `all` | All extras combined |

---

## Stability Contract

- Core tables are system-neutral and partitioned by `system_id`.
- System packages extend records through namespaced JSON data or uniquely named extension tables.
- Optional vector and embedding dependencies are imported lazily.
- A runtime activates exactly one system profile.
- This is a new project and carries no legacy database compatibility contract.

---

## Scene Metadata Ownership

A parsed scene carries both column-backed fields (always present) and a `metadata_json` JSON dict populated by the system profile at parse time. Consumers should treat this as a **best-effort enrichment** rather than a guaranteed schema.

| Field | Source | Always present? |
|-------|--------|----------------|
| `scene_type` | `ModuleScene.scene_type` column | Yes |
| `headings` | `ModuleScene.headings` column | Yes |
| `scene_level`, `line_count`, `subsections`, `tags` | Any profile implementing `scene_boundaries()` | If profile does |
| `visibility` | Profile metadata, defaults to `"keeper"` | Defaulted |
| `clues`, `checks` | CoC profile (`CocModuleProfile`) | No |
| `sanity` | CoC profile only | No |
| `transitions`, `node_id` | CoC solo-scenario parsing only | No |

System packages choose which fields their profile writes. A profile that omits an enrichment is **not** a bug — callers must check for empty lists / `None` rather than assuming the field carries meaning for that system.

---

## Development

```bash
pip install -e ".[all,dev]"
pytest --cov
ruff check .
```

---

## License

MIT
