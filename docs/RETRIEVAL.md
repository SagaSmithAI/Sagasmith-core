# Retrieval and Embeddings

Rules and modules use the same retrieval pipeline:

1. exact source or heading-title matches;
2. language-neutral lexical scoring, including CJK characters and bigrams;
3. dense cosine retrieval;
4. reciprocal-rank fusion across available rankings;
5. expansion from a selected chunk to the complete section or scene.

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

There are two integrity-checked, content-addressed cache layers: raw page extraction
(including OCR) and the final normalized document. A parser/heading version change
invalidates only final normalization, allowing verified page/OCR work to be reused.

Dense retrieval is optional. SQL JSON vectors provide a small-dataset fallback;
`VectorStore` provides a namespaced ChromaDB implementation for larger stores.

## Built-in BGE profiles

| Key | Model | Dimensions | Routing |
|---|---|---:|---|
| `bge_m3` | `BAAI/bge-m3` | 1024 | multilingual default |
| `bge_small_zh_v1_5` | `BAAI/bge-small-zh-v1.5` | 512 | Chinese |
| `bge_small_en_v1_5` | `BAAI/bge-small-en-v1.5` | 384 | English |

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

ChromaDB remains model-isolated through profile-suffixed collection names, so
vectors with different dimensions cannot be mixed.

