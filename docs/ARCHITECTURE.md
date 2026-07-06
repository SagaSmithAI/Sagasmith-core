# General TTRPG Base Architecture

`sagasmith-core` is a new project. It does not preserve the schema or runtime
behavior of earlier SagaSmith repositories.

## Domain boundary

Core owns system-neutral concepts:

- campaign identity, settings, and mutable state;
- characters and extensible character sheets;
- rule sources, hierarchical sections, retrieval chunks, and embeddings;
- module sources, chapters, scenes, retrieval chunks, and scene progress scoped
  to a party, split group, or individual player;
- parser and system-plugin protocols;
- transactional database, migrations, and optional vector storage.

System packages own game semantics:

- dice and checks;
- combat and advancement rules;
- system-specific character-sheet validation;
- rule terminology and parser enrichments;
- agent tools, skills, identity, and presentation.

## Extension policy

All common records carry `system_id`. System-specific fields should first use a
namespaced JSON object:

```json
{
  "dnd": {"armor_class": 16, "level": 3}
}
```

A system may add uniquely named extension tables when relational constraints
are required, for example `dnd_spell_slots`. It must not redefine or shadow a
core table.

## Integration direction

```text
sagasmith-dnd ─┐
sagasmith-coc ├─> sagasmith-core
custom-system ─┘
```

Core has no Agent-platform adapter. nanobot and other platforms load a
system-specific Skill and call the system Runtime's JSON CLI through their
normal shell capability.

Scene progress uses a stable `scope_id`: `party`, `group:<id>`, or
`player:<character-id>`. A scoped current-scene read may inherit `party` until
that scope records its own scene. Writes and current-scene replacement affect
only the selected scope.

## Scene metadata ownership

A parsed scene carries both column-backed fields (always present) and a
`metadata_json` JSON dict populated by the system profile at parse time.
Consumers should treat this as a **best-effort enrichment** rather than a
guaranteed schema.

| Field | Source | Always present? |
|-------|--------|----------------|
| `scene_type` | `ModuleScene.scene_type` column | Yes |
| `headings` | `ModuleScene.headings` column | Yes |
| `scene_level`, `line_count`, `subsections`, `tags` | Any profile implementing `scene_boundaries()` | If profile does |
| `visibility` | Profile metadata, defaults to `"keeper"` | Defaulted |
| `clues`, `checks` | CoC profile (`CocModuleProfile`) | No |
| `sanity` | CoC profile only | No |
| `transitions`, `node_id` | CoC solo-scenario parsing only | No |

System packages (e.g. `sagasmith-dnd`, `sagasmith-coc`) choose which fields
their profile writes. A profile that omits an enrichment is **not** a bug —
callers must check for empty lists / `None` rather than assuming the field
carries meaning for that system.
