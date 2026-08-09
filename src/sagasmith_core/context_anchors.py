"""Strict, non-executable module-context anchors for Agent-as-DM context."""

from __future__ import annotations

import re
from typing import Any

from sagasmith_core.modules import (
    EXACT_MODULE_SOURCE_FIELD_ORDER,
    canonical_heading_path,
    clean_source_evidence_text,
    normalize_source_evidence_text,
)

CONTEXT_ANCHOR_KIND = "context_anchor"
CONTEXT_ANCHOR_SCHEMA_VERSION = 1
CONTEXT_ENTITY_KINDS = frozenset(
    {
        "actor",
        "faction",
        "item",
        "location",
        "module",
        "quest",
        "scene",
    }
)
MAX_CONTEXT_ANCHOR_BINDINGS = 16
MAX_CONTEXT_ANCHOR_RELATED_REFS = 64
MAX_CONTEXT_SOURCE_EXCERPT_CHARS = 2_000
MAX_PINNED_MODULE_EVIDENCE_CHARS = 100_000

_ENTITY_REF = re.compile(rf"^({'|'.join(sorted(CONTEXT_ENTITY_KINDS))}):([^\s:][^\s]{{0,279}})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize_context_entity_ref(value: Any, *, field: str = "context_ref") -> str:
    """Validate one opaque entity link without assigning narrative semantics."""

    normalized = str(value or "").strip()
    if _ENTITY_REF.fullmatch(normalized) is None:
        raise ValueError(f"{field} must use <actor|faction|item|location|module|quest|scene>:<id>")
    return normalized


def normalize_context_anchor_metadata(
    value: Any,
    *,
    subject_ref: str,
    predicate: str,
    disclosure_scope: str,
) -> dict[str, Any]:
    """Normalize an anchor that can retrieve evidence but cannot encode behavior."""

    if not isinstance(value, dict):
        raise ValueError("context_anchor metadata must be an object")
    allowed = {
        "schema_version",
        "purpose",
        "related_refs",
        "source_bindings",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            "context_anchor metadata has unsupported fields: " + ", ".join(sorted(unknown))
        )
    if int(value.get("schema_version", 0) or 0) != CONTEXT_ANCHOR_SCHEMA_VERSION:
        raise ValueError(f"context_anchor schema_version must be {CONTEXT_ANCHOR_SCHEMA_VERSION}")
    if str(predicate or "").strip():
        raise ValueError("context_anchor cannot define a predicate or executable trigger")
    if disclosure_scope != "dm":
        raise ValueError("context_anchor must be DM-only")

    normalized_subject = normalize_context_entity_ref(
        subject_ref,
        field="context_anchor subject_ref",
    )
    purpose = " ".join(str(value.get("purpose") or "").split())
    if not 1 <= len(purpose) <= 200:
        raise ValueError("context_anchor purpose must contain 1 to 200 characters")

    raw_related = value.get("related_refs")
    if not isinstance(raw_related, list):
        raise ValueError("context_anchor related_refs must be a list")
    related = [
        normalize_context_entity_ref(item, field="context_anchor related_refs[]")
        for item in raw_related
    ]
    related = list(dict.fromkeys([normalized_subject, *related]))
    if len(related) > MAX_CONTEXT_ANCHOR_RELATED_REFS:
        raise ValueError(
            f"context_anchor related_refs cannot exceed {MAX_CONTEXT_ANCHOR_RELATED_REFS} entries"
        )

    raw_bindings = value.get("source_bindings")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise ValueError("context_anchor source_bindings must be a non-empty list")
    if len(raw_bindings) > MAX_CONTEXT_ANCHOR_BINDINGS:
        raise ValueError(
            f"context_anchor source_bindings cannot exceed {MAX_CONTEXT_ANCHOR_BINDINGS} entries"
        )
    bindings = [
        normalize_context_source_binding(item, field=f"source_bindings[{index}]")
        for index, item in enumerate(raw_bindings)
    ]
    return {
        "schema_version": CONTEXT_ANCHOR_SCHEMA_VERSION,
        "purpose": purpose,
        "related_refs": related,
        "source_bindings": bindings,
    }


def normalize_context_source_binding(
    value: Any,
    *,
    field: str = "source_binding",
) -> dict[str, Any]:
    """Validate one exact module chunk plus a verbatim, bounded excerpt."""

    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    unknown = set(value) - {"source_ref", "source_excerpt"}
    if unknown:
        raise ValueError(f"{field} has unsupported fields: {sorted(unknown)}")
    source_ref = value.get("source_ref")
    if not isinstance(source_ref, dict):
        raise ValueError(f"{field}.source_ref must be an object")
    missing = set(EXACT_MODULE_SOURCE_FIELD_ORDER) - set(source_ref)
    unknown_source = set(source_ref) - set(EXACT_MODULE_SOURCE_FIELD_ORDER)
    if missing or unknown_source:
        raise ValueError(
            f"{field}.source_ref must contain exactly " + ", ".join(EXACT_MODULE_SOURCE_FIELD_ORDER)
        )
    normalized_source = {
        "module_id": _required_text(source_ref.get("module_id"), f"{field}.module_id"),
        "scene_id": _required_text(source_ref.get("scene_id"), f"{field}.scene_id"),
        "chunk_id": _required_text(source_ref.get("chunk_id"), f"{field}.chunk_id"),
        "page_start": _optional_page(source_ref.get("page_start"), f"{field}.page_start"),
        "page_end": _optional_page(source_ref.get("page_end"), f"{field}.page_end"),
        "heading_path": list(
            canonical_heading_path(
                _string_list(source_ref.get("heading_path"), f"{field}.heading_path")
            )
        ),
        "content_sha256": _required_text(
            source_ref.get("content_sha256"),
            f"{field}.content_sha256",
        ).casefold(),
    }
    if (
        normalized_source["page_start"] is not None
        and normalized_source["page_end"] is not None
        and normalized_source["page_end"] < normalized_source["page_start"]
    ):
        raise ValueError(f"{field}.page_end cannot precede page_start")
    if _SHA256.fullmatch(normalized_source["content_sha256"]) is None:
        raise ValueError(f"{field}.content_sha256 must be a lowercase SHA-256")
    excerpt = clean_source_evidence_text(value.get("source_excerpt"))
    if not 1 <= len(excerpt) <= MAX_CONTEXT_SOURCE_EXCERPT_CHARS:
        raise ValueError(
            f"{field}.source_excerpt must contain 1 to "
            f"{MAX_CONTEXT_SOURCE_EXCERPT_CHARS} characters"
        )
    return {
        "source_ref": normalized_source,
        "source_excerpt": excerpt,
    }


def resolve_context_source_binding(
    binding: dict[str, Any],
    *,
    expanded: dict[str, Any],
    campaign_id: str,
) -> dict[str, Any]:
    """Prove that one persisted binding still matches its immutable module chunk."""

    normalized = normalize_context_source_binding(binding)
    source_ref = normalized["source_ref"]
    expanded_ref = dict(expanded.get("source_ref") or {})
    if str(expanded.get("campaign_id") or "") != campaign_id:
        raise ValueError("context_anchor source chunk belongs to another campaign")
    expected_ref = {key: expanded_ref.get(key) for key in EXACT_MODULE_SOURCE_FIELD_ORDER}
    expected_ref["heading_path"] = list(
        canonical_heading_path(expected_ref.get("heading_path") or [])
    )
    if source_ref != expected_ref:
        raise ValueError("context_anchor source_ref no longer matches its module chunk")
    excerpt = normalized["source_excerpt"]
    if normalize_source_evidence_text(excerpt) not in normalize_source_evidence_text(
        expanded.get("content")
    ):
        raise ValueError("context_anchor source_excerpt is absent from its module chunk")
    return normalized


def _required_text(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 300:
        raise ValueError(f"{field} must contain 1 to 300 characters")
    return normalized


def _optional_page(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be null or a positive integer")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    normalized = [str(item).strip() for item in value]
    if any(not item for item in normalized):
        raise ValueError(f"{field} entries must not be empty")
    return normalized


__all__ = [
    "CONTEXT_ANCHOR_KIND",
    "CONTEXT_ANCHOR_SCHEMA_VERSION",
    "MAX_PINNED_MODULE_EVIDENCE_CHARS",
    "normalize_context_anchor_metadata",
    "normalize_context_entity_ref",
    "normalize_context_source_binding",
    "resolve_context_source_binding",
]
