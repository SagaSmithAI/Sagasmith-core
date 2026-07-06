"""Exact, lexical, dense, and reciprocal-rank-fusion retrieval helpers.

Structured scoring and query expansion
--------------------------------------
When ChromaDB / embeddings are unavailable, ``structured_score()`` and
``enrich_query()`` compensate by leveraging your data's existing structure:

- **Multi-field scoring** \u2014 ``scene_title``, ``headings``, ``keywords``,
  ``tags``, ``scene_type``, ``chunk_type``, and more each carry different
  weights so a short but precise match beats a long loose one.
- **Query expansion** \u2014 ``enrich_query()`` appends English equivalents for
  common Chinese TTRPG terms, bridging the gap between player language and
  English-heavy rule/module text without any vector model.

Together they replace the old single-field ``lexical_score()`` with a
profile-aware, denormalised scoring pipeline that works on any backend
(SQLite, PostgreSQL) with zero additional dependencies.

SQLite FTS5 full-text search
----------------------------
When running on SQLite, ``fts5_hits()`` provides indexed BM25 ranking via
the ``module_fts`` / ``rule_fts`` virtual tables created by migration
``20260706_04``.  FTS5 replaces the O(n) Python-side structured_score()
with an O(log n) indexed search that also handles phrase matching and
BM25 normalisation \u2014 all with zero pip dependencies.

Callers check the dialect name and call ``fts5_hits()`` first; if it
returns a non-empty list, those chunk IDs become the ``"lexical"``
channel in the reciprocal-rank-fusion pipeline.  Otherwise they fall
back to ``structured_score()`` as before.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

_LATIN_WORD = re.compile(r"[A-Za-z0-9_'-]+")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

# \u2500\u2500 Built-in Chinese \u2194 English query expansions \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# System-neutral TTRPG terms. System profiles (D&D, CoC) can add their
# own domain vocabulary via the ``query_hints`` parameter on search().
_TTRPG_TERMS: dict[str, Sequence[str]] = {
    "\u8c41\u514d": ("save", "saving"),
    "\u68c0\u5b9a": ("check", "roll", "test"),
    "\u5c5e\u6027": ("ability", "score", "stat"),
    "\u6280\u80fd": ("skill",),
    "\u719f\u7ec3": ("proficient", "proficiency"),
    "\u653b\u51fb": ("attack", "strike"),
    "\u4f24\u5bb3": ("damage", "wound", "hurt"),
    "\u9632\u5fa1": ("defense", "armor", "protect"),
    "\u6cbb\u7597": ("heal", "healing", "cure"),
    "\u6cd5\u672f": ("spell", "magic"),
    "\u6b66\u5668": ("weapon", "arms"),
    "\u62a4\u7532": ("armor", "armour"),
    "\u9ab0\u5b50": ("dice", "roll"),
    "\u7b49\u7ea7": ("level",),
    "\u7ecf\u9a8c": ("experience", "xp"),
    "\u7ebf\u7d22": ("clue", "hint", "evidence"),
    "\u6218\u6597": ("combat", "battle", "fight"),
    "\u8425\u5730": ("camp", "rest"),
    "\u7269\u54c1": ("item", "object", "thing"),
    "\u95e8": ("door", "gate", "entrance"),
    "\u94a5\u5319": ("key",),
    "\u5b9d\u85cf": ("treasure", "loot"),
    "\u9677\u9631": ("trap", "hazard"),
    "\u602a\u7269": ("monster", "creature", "beast"),
    "\u5934\u76ee": ("boss", "leader", "chief"),
    "\u4efb\u52a1": ("quest", "mission", "task"),
    "\u5956\u52b1": ("reward", "prize"),
    "\u56de\u5408": ("turn", "round"),
    "\u79fb\u52a8": ("move", "movement"),
    "\u641c\u7d22": ("search", "explore", "scan"),
    "\u9690\u85cf": ("hidden", "secret", "conceal"),
}

# Default field weights for structured scoring.
# Used when the caller does not supply custom ``field_weights``.
_STRUCTURED_WEIGHTS: dict[str, float] = {
    "module_title": 8.0,
    "chapter_title": 6.0,
    "source_title": 5.0,
    "section_title": 5.0,
    "scene_title": 4.0,
    "heading_paths": 3.0,
    "keywords": 2.5,
    "tags": 2.0,
    "scene_type": 2.0,
    "chunk_type": 1.5,
    "content": 1.0,
}


@dataclass(frozen=True)
class SearchHit:
    id: str
    score: float
    title: str
    content: str
    source_id: str
    heading_path: tuple[str, ...] = ()
    retrieval: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


def terms(text: str) -> list[str]:
    normalized = text.casefold()
    values = _LATIN_WORD.findall(normalized)
    cjk = _CJK.findall(normalized)
    values.extend(cjk)
    values.extend("".join(cjk[index : index + 2]) for index in range(len(cjk) - 1))
    return [value for value in values if value]


def lexical_score(query: str, *, title: str, content: str) -> float:
    """Legacy single-field scorer \u2014 kept for backward compatibility.

    Prefer ``structured_score()`` in new code; it accepts multiple weighted
    fields (headings, keywords, tags, \u2026) and produces significantly better
    rankings when those fields are populated.
    """
    query_terms = terms(query)
    if not query_terms:
        return 0.0
    title_folded = title.casefold()
    content_folded = content.casefold()
    score = 0.0
    for term in query_terms:
        score += title_folded.count(term) * 4.0
        score += min(content_folded.count(term), 8)
    return score / math.sqrt(max(len(terms(content)), 1))


def enrich_query(
    query: str,
    *,
    extra_terms: dict[str, Sequence[str]] | None = None,
) -> str:
    """Expand Chinese TTRPG terms with English equivalents.

    Appends English aliases behind the original query so both Chinese and
    English lexical matching fire on the same search.  Built-in mappings
    are system-neutral; system profiles inject domain vocabulary (e.g.
    D&D-specific terms like ``"\u8c41\u514d" \u2192 "saving throw"``) via
    ``extra_terms``.

    Returns the original query unchanged when no expansions fire.
    """
    merged = dict(_TTRPG_TERMS)
    if extra_terms:
        for term, aliases in extra_terms.items():
            merged.setdefault(term, []).extend(aliases)

    expansions: list[str] = []
    for term, aliases in merged.items():
        if term in query:
            expansions.extend(aliases)

    if expansions:
        return f"{query} {' '.join(dict.fromkeys(expansions))}"
    return query


def structured_score(
    query: str,
    *,
    module_title: str = "",
    chapter_title: str = "",
    scene_title: str = "",
    section_title: str = "",
    source_title: str = "",
    heading_paths: str = "",
    keywords: str = "",
    tags: str = "",
    scene_type: str = "",
    chunk_type: str = "",
    content: str = "",
    field_weights: dict[str, float] | None = None,
) -> float:
    """Multi-field weighted relevance score for structured TTRPG data.

    Each named field carries a default weight (see ``_STRUCTURED_WEIGHTS``)
    that can be overridden via ``field_weights``.  Passing an empty string
    for a field skips it entirely.

    Normalisation divides by ``sqrt(max(content_terms, 4))`` so that short
    but precise documents are not drowned out by long rambling ones, while
    avoiding division by tiny numbers on metadata-only documents.
    """
    query_terms = terms(query)
    if not query_terms:
        return 0.0

    weights = field_weights if field_weights is not None else _STRUCTURED_WEIGHTS
    fields = {
        "module_title": module_title,
        "chapter_title": chapter_title,
        "scene_title": scene_title,
        "section_title": section_title,
        "source_title": source_title,
        "heading_paths": heading_paths,
        "keywords": keywords,
        "tags": tags,
        "scene_type": scene_type,
        "chunk_type": chunk_type,
        "content": content,
    }

    score = 0.0
    for field_name, field_value in fields.items():
        if not field_value:
            continue
        weight = weights.get(field_name, 1.0)
        folded = field_value.casefold()
        for term in query_terms:
            score += folded.count(term) * weight

    content_terms = len(terms(content)) if content else 1
    return score / math.sqrt(max(content_terms, 4))


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def reciprocal_rank_fusion(
    rankings: dict[str, list[str]],
    *,
    weights: dict[str, float] | None = None,
    rank_constant: int = 60,
) -> list[tuple[str, float, tuple[str, ...]]]:
    scores: dict[str, float] = {}
    sources: dict[str, list[str]] = {}
    for name, ids in rankings.items():
        weight = (weights or {}).get(name, 1.0)
        for rank, item_id in enumerate(ids, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + weight / (rank_constant + rank)
            sources.setdefault(item_id, []).append(name)
    return sorted(
        (
            (item_id, score, tuple(sources[item_id]))
            for item_id, score in scores.items()
        ),
        key=lambda item: (-item[1], item[0]),
    )


# ── SQLite FTS5 helpers ──────────────────────────────────────────────
# These are used by RuleService.search() and ModuleService.search() when
# running on SQLite.  They require the FTS5 virtual tables created by
# migration 20260706_04.  PostgreSQL callers silently skip to the
# structured_score fallback.

_FTS5_SPECIAL = re.compile(r"[*^\"()+\-\\]")


def fts5_query(query: str) -> str | None:
    """Convert a plain-text user query to an FTS5 MATCH expression.

    * English word tokens are kept as-is.
    * Chinese characters are each emitted as independent tokens with a
      mandatory ``+`` prefix so the result requires ALL CJK characters
      from the query.
    * FTS5 special characters (``* ^ \\" ( ) + - \\``) are stripped.
    * Returns ``None`` when the query contains no valid tokens (useful
      for early-exit).
    """
    stripped = _FTS5_SPECIAL.sub(" ", query).strip()
    if not stripped:
        return None

    terms_builder: list[str] = []

    # Extract CJK character runs — each becomes a mandatory + token
    last = 0
    for cjk_match in _CJK.finditer(stripped):
        start = cjk_match.start()
        # Any Latin text before this CJK run?
        if start > last:
            latin_bit = stripped[last:start]
            for word in _LATIN_WORD.finditer(latin_bit):
                terms_builder.append(word.group())
        # Each CJK character in the run gets + prefix
        run = cjk_match.group()
        for char in run:
            if not char.isspace():
                terms_builder.append(f"+{char}")
        last = cjk_match.end()

    # Remaining Latin text after last CJK run
    tail = stripped[last:]
    if tail:
        for word in _LATIN_WORD.finditer(tail):
            terms_builder.append(word.group())

    if not terms_builder:
        return None
    return " ".join(dict.fromkeys(terms_builder))


def fts5_hits(
    session,
    table: str,
    query: str,
    *,
    limit: int = 20,
    weights: tuple[float, ...] | None = None,
) -> list[str]:
    """Run an FTS5 MATCH and return chunk IDs ranked by BM25.

    ``table`` is the FTS virtual-table name (``"module_fts"`` or
    ``"rule_fts"``).  ``weights`` are per-column BM25 weights matching
    the column order declared in the FTS5 schema (including the
    ``chunk_id UNINDEXED`` column — pass ``0`` for that one).

    Returns an empty list when FTS5 is unavailable (not SQLite,
    migration not applied, empty index) so callers can fall through
    to ``structured_score()`` unconditionally.
    """
    if session.bind.dialect.name != "sqlite":
        return []
    match_expr = fts5_query(query)
    if match_expr is None:
        return []
    try:
        if weights:
            weights_str = ", ".join(str(w) for w in weights)
            order_clause = f"bm25({table}, {weights_str})"
        else:
            order_clause = "rank"
        rows = session.execute(
            __import__("sqlalchemy").text(
                f"SELECT chunk_id FROM {table} "
                f"WHERE {table} MATCH :query "
                f"ORDER BY {order_clause} LIMIT :limit"
            ),
            {"query": match_expr, "limit": limit},
        )
        return [str(row[0]) for row in rows]
    except Exception:
        # Table may not exist (migration not yet applied)
        return []

