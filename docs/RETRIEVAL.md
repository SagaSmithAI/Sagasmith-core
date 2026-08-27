# Retrieval and Embeddings

Rules and modules use the same retrieval pipeline:

1. exact source or heading-title matches;
2. language-neutral lexical scoring, including CJK characters and bigrams;
3. dense cosine retrieval;
4. reciprocal-rank fusion across available rankings;
5. expansion from a selected chunk to the complete section or scene.

Core does not ship game vocabulary or bilingual rule synonyms. System packages
pass their own deterministic `query_hints` to `RuleService.search` or
`ModuleService.search`; `enrich_query` only applies the supplied mapping.

`RuleService.search` can be constrained with exact `source_ids`, `source_keys`, or
publication ids. Import and review workflows should use one of these filters before
expanding a hit so same-name sections from different books or editions cannot supply
the wrong evidence.

## Document normalization

PDF imports use PDFium for the text layer and pypdf only for the outline. Page markers
are indexed once, so page lookup remains logarithmic as large books are chunked. The
quality report records sparse, corrupt-text, and OCR-recovered pages. With the `ocr`
extra installed, RapidOCR is applied selectively to image-only documents and corrupt
text pages; unresolved low-quality documents fail closed.

Dense embedding callers may set `<PREFIX>_EMBEDDING_CACHE_DIR` or pass
`cache_dir=` to `BgeEmbedder`. The embedder checks its bounded process-local LRU
before a process-safe SQLite cache. Each row is addressed by a structured SHA-256
identity covering the cache schema, immutable model revision, profile, dimensions,
inference epoch, and exact encoded text; only a verified float32 vector is stored.
Dimension, type, checksum, or finite-value failures are treated as misses. SQLite
lock waits default to 50 ms and a failed read suppresses the matching write attempt,
so model inference remains available when persistence is busy or unavailable.

The persistent layer is bounded by both entry count (50,000 by default) and logical
bytes (256 MiB by default), with oldest-write eviction. Use the matching
`<PREFIX>_EMBEDDING_CACHE_MAX_ENTRIES`, `_MAX_BYTES`, `_BUSY_TIMEOUT_MS`, and
`_EPOCH` settings to tune it. Built-in BGE profiles pin immutable Hub commits;
custom profiles must supply `model_revision` as a 40-character commit SHA before
their identity can be used by SQL, Chroma, or the persistent cache. Store each cache
outside source checkouts with private OS permissions and tenant isolation. New POSIX
cache paths are restricted to the current user automatically, but callers still own
their parent-directory and backup policy: text digests can be dictionary-guessed and
embeddings remain sensitive derived data even though raw text is not stored.

There are two integrity-checked, content-addressed cache layers: raw page extraction
(including OCR) and the final normalized document. A parser/heading version change
invalidates only final normalization, allowing verified page/OCR work to be reused.

Dense retrieval is optional. SQL JSON vectors provide a small-dataset fallback;
`VectorStore` provides a namespaced ChromaDB implementation for larger stores.
The transactional vector outbox flushes only jobs whose stored embedding identity
matches the requested profile revision; jobs for another revision remain pending
for their matching collection instead of being marked complete in the wrong one.

## Built-in BGE profiles

| Key | Model | Pinned revision | Dimensions | Routing |
|---|---|---|---:|---|
| `bge_m3` | `BAAI/bge-m3` | `5617a9f61b02` | 1024 | multilingual default |
| `bge_small_zh_v1_5` | `BAAI/bge-small-zh-v1.5` | `7999e1d33597` | 512 | Chinese |
| `bge_small_en_v1_5` | `BAAI/bge-small-en-v1.5` | `5c38ec7c405e` | 384 | English |

Configure one or more profiles per system:

```bash
DND5E_EMBEDDING_PROFILES=bge_small_zh_v1_5,bge_small_en_v1_5
DND5E_EMBEDDING_MODE=auto
DND5E_EMBEDDING_BATCH_SIZE=8
```

When multiple profiles are configured, Chinese and English text route to their
language-specific small model; mixed-language text falls back to the first
multilingual profile when present.

```python
from sagasmith_core import create_embedder

embedder = create_embedder(env_prefix="DND5E", language="zh-CN")
vectors = embedder.encode(["擒抱规则"])
```

SQL rows and ChromaDB collections use the revision-scoped model identity. Chroma
collection names include a structured identity digest, so a model revision or
dimension change creates a new index instead of mixing old and new vectors.
