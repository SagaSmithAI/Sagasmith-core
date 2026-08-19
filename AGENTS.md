# SagaSmith Core Agent Guide

## Scope

`sagasmith-core` owns only system-neutral persistence, documents, retrieval,
revisions, transactions, content-package primitives, and campaign continuity.
It must not absorb D&D-, CoC-, Narrative-, book-, module-, or campaign-specific
interpretation.

## Current consumers

- D&D: <https://github.com/SagaSmithAI/sagasmith-dnd>
- Call of Cthulhu: <https://github.com/SagaSmithAI/sagasmith-coc>
- Narrative: <https://github.com/SagaSmithAI/sagasmith-narrative>

Each current domain repository owns its Domain package, MCP server, Skills, and
UI where applicable. Former standalone MCP, Skills, UI, and Module Generator
repositories are archived history and must not become dependencies or fallback
protocols.

## Change boundaries

- Put reusable deterministic mechanics in the relevant domain package.
- Put authoritative state, authorization, random streams, revisions,
  idempotency, and atomic settlement in the relevant MCP server.
- Put semantic review and reusable Agent procedures in Skills.
- Keep book-specific decisions inside the Pack draft, evidence, fixture, or
  metadata that justifies them.
- Preserve one current public protocol; remove superseded compatibility paths
  unless an explicit external commitment requires them.

## Validation

```powershell
pip install -e ".[dev]"
pytest
ruff check .
```

Run tests through public services and migrations proportional to the changed
boundary. Do not weaken evidence, authorization, or transaction guarantees to
make a regression pass.
