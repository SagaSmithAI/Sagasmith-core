# General TTRPG Base Architecture

`sagasmith-core` is a new project. It does not preserve the schema or runtime
behavior of earlier SagaSmith repositories.

## Domain boundary

Core owns system-neutral concepts:

- campaign identity, settings, and mutable state;
- characters and extensible character sheets;
- rule sources, hierarchical sections, retrieval chunks, and embeddings;
- module sources, chapters, scenes, retrieval chunks, and scene progress;
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
