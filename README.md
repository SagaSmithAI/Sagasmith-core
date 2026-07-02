# SagaSmith Core

`sagasmith-core` is a general TTRPG application base. It contains no D&D or
Call of Cthulhu rules. System packages register rules and CLIs on top of a
shared set of durable services:

- campaigns and system-neutral campaign state;
- characters with extensible sheets;
- rule documents, sections, chunks, ingestion, and search;
- adventure modules, chapters, scenes, chunks, and scene progress;
- SQLAlchemy transactions and bundled migrations;
- optional ChromaDB and embedding infrastructure;
- immutable snapshot DAGs, audited revisions, and branch-aware memory.

## Install

```bash
pip install sagasmith-core
```

System packages normally install it automatically. Core has no Agent-platform
dependency.

## Stability contract

- Core tables are system-neutral and partitioned by `system_id`.
- System packages extend records through namespaced JSON data or uniquely
  named extension tables.
- Optional vector and embedding dependencies are imported lazily.
- A runtime activates exactly one system profile.
- This is a new project and carries no legacy database compatibility contract.
