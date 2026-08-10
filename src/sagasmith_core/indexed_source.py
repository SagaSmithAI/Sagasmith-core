"""Detached indexed rule-source contract used while building content packages."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Mapping

from sagasmith_core.parsing import MAX_RULE_SECTION_TITLE_CHARS

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RuleSourceContractError(ValueError):
    """Raised when an indexed source contains unstable or invalid data."""


def rule_chunk_key(
    source_key: str,
    section_ordinal: int,
    chunk_ordinal: int,
    content: str,
) -> str:
    """Return the stable content address for an indexed rule chunk."""

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"{source_key}/section-{section_ordinal}/chunk-{chunk_ordinal}-{digest}"


def _validate_indexed_rule_source(value: Any, index: int) -> dict[str, Any]:
    field = f"rule_pack.sources[{index}]"
    if not isinstance(value, dict):
        raise RuleSourceContractError(f"{field} must be an object")
    allowed = {
        "source_key",
        "title",
        "edition",
        "locale",
        "version",
        "publication_id",
        "authority",
        "canonical_source_key",
        "checksum",
        "metadata",
        "sections",
    }
    _exact_fields(value, allowed, field)
    source_key = _required_text(value["source_key"], f"{field}.source_key", maximum=200)
    _required_text(value["title"], f"{field}.title", maximum=300)
    for name, maximum in (
        ("edition", 64),
        ("locale", 32),
        ("version", 100),
        ("publication_id", 200),
        ("authority", 32),
    ):
        if not isinstance(value[name], str) or len(value[name]) > maximum:
            raise RuleSourceContractError(
                f"{field}.{name} must be a string up to {maximum} characters"
            )
    canonical = value["canonical_source_key"]
    if canonical is not None:
        _required_text(canonical, f"{field}.canonical_source_key", maximum=200)
    checksum = _required_text(value["checksum"], f"{field}.checksum", maximum=64)
    if not _SHA256_RE.fullmatch(checksum):
        raise RuleSourceContractError(f"{field}.checksum must be a lowercase SHA-256")
    metadata = value["metadata"]
    if not isinstance(metadata, dict):
        raise RuleSourceContractError(f"{field}.metadata must be an object")
    _reject_rule_source_runtime_metadata(metadata, f"{field}.metadata")
    sections = value["sections"]
    if not isinstance(sections, list) or not sections:
        raise RuleSourceContractError(f"{field}.sections must be a non-empty array")
    section_ordinals: set[int] = set()
    for section_index, section in enumerate(sections):
        section_field = f"{field}.sections[{section_index}]"
        if not isinstance(section, dict):
            raise RuleSourceContractError(f"{section_field} must be an object")
        _exact_fields(
            section,
            {
                "ordinal",
                "parent_ordinal",
                "level",
                "title",
                "path",
                "content",
                "content_hash",
                "start_offset",
                "end_offset",
                "chunks",
            },
            section_field,
        )
        ordinal = section["ordinal"]
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or ordinal in section_ordinals
        ):
            raise RuleSourceContractError(
                f"{section_field}.ordinal must be a unique non-negative integer"
            )
        section_ordinals.add(ordinal)
        parent = section["parent_ordinal"]
        if parent is not None and (
            isinstance(parent, bool)
            or not isinstance(parent, int)
            or parent < 0
            or parent >= ordinal
        ):
            raise RuleSourceContractError(
                f"{section_field}.parent_ordinal must reference an earlier section"
            )
        level = section["level"]
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 6:
            raise RuleSourceContractError(f"{section_field}.level must be from 1 to 6")
        _required_text(
            section["title"],
            f"{section_field}.title",
            maximum=MAX_RULE_SECTION_TITLE_CHARS,
        )
        if (
            not isinstance(section["path"], list)
            or not section["path"]
            or any(not isinstance(item, str) or not item.strip() for item in section["path"])
        ):
            raise RuleSourceContractError(f"{section_field}.path must be a non-empty string array")
        content = section["content"]
        if not isinstance(content, str):
            raise RuleSourceContractError(f"{section_field}.content must be a string")
        if section["content_hash"] != hashlib.sha256(content.encode("utf-8")).hexdigest():
            raise RuleSourceContractError(f"{section_field}.content_hash mismatch")
        _validate_offsets(section, section_field)
        if section["end_offset"] - section["start_offset"] != len(content):
            raise RuleSourceContractError(
                f"{section_field} offsets do not match its content length"
            )
        chunks = section["chunks"]
        if not isinstance(chunks, list) or not chunks:
            raise RuleSourceContractError(f"{section_field}.chunks must be a non-empty array")
        chunk_ordinals: set[int] = set()
        for chunk_index, chunk in enumerate(chunks):
            chunk_field = f"{section_field}.chunks[{chunk_index}]"
            if not isinstance(chunk, dict):
                raise RuleSourceContractError(f"{chunk_field} must be an object")
            _exact_fields(
                chunk,
                {
                    "key",
                    "ordinal",
                    "heading_path",
                    "content",
                    "content_hash",
                    "token_count",
                    "metadata",
                },
                chunk_field,
            )
            chunk_ordinal = chunk["ordinal"]
            if (
                isinstance(chunk_ordinal, bool)
                or not isinstance(chunk_ordinal, int)
                or chunk_ordinal < 0
                or chunk_ordinal in chunk_ordinals
            ):
                raise RuleSourceContractError(
                    f"{chunk_field}.ordinal must be unique in its section and non-negative"
                )
            chunk_ordinals.add(chunk_ordinal)
            chunk_content = chunk["content"]
            if not isinstance(chunk_content, str):
                raise RuleSourceContractError(f"{chunk_field}.content must be a string")
            chunk_hash = hashlib.sha256(chunk_content.encode("utf-8")).hexdigest()
            if chunk["content_hash"] != chunk_hash:
                raise RuleSourceContractError(f"{chunk_field}.content_hash mismatch")
            expected_key = rule_chunk_key(
                source_key, ordinal, chunk_ordinal, chunk_content
            )
            if chunk["key"] != expected_key:
                raise RuleSourceContractError(f"{chunk_field}.key must be {expected_key!r}")
            if not isinstance(chunk["heading_path"], list) or any(
                not isinstance(item, str) or not item.strip() for item in chunk["heading_path"]
            ):
                raise RuleSourceContractError(f"{chunk_field}.heading_path must be a string array")
            token_count = chunk["token_count"]
            if isinstance(token_count, bool) or not isinstance(token_count, int) or token_count < 0:
                raise RuleSourceContractError(f"{chunk_field}.token_count must be non-negative")
            if not isinstance(chunk["metadata"], dict):
                raise RuleSourceContractError(f"{chunk_field}.metadata must be an object")
            _reject_rule_source_runtime_metadata(chunk["metadata"], f"{chunk_field}.metadata")
    missing_parents = sorted(
        int(section["parent_ordinal"])
        for section in sections
        if section["parent_ordinal"] is not None
        and int(section["parent_ordinal"]) not in section_ordinals
    )
    if missing_parents:
        raise RuleSourceContractError(
            f"{field} references missing parent sections: {missing_parents}"
        )
    return copy.deepcopy(value)


def validate_indexed_rule_source(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one detached indexed rule source used by Pack construction."""

    return _validate_indexed_rule_source(value, 0)


def _reject_rule_source_runtime_metadata(value: Any, field: str) -> None:
    if isinstance(value, dict):
        forbidden = {
            key
            for key in value
            if key
            in {
                "chunk_id",
                "import_job_id",
                "section_id",
                "source_id",
                "source_path",
            }
        }
        if forbidden:
            raise RuleSourceContractError(
                f"{field} contains machine-local fields: " + ", ".join(sorted(forbidden))
            )
        for key, child in value.items():
            _reject_rule_source_runtime_metadata(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_rule_source_runtime_metadata(child, f"{field}[{index}]")


def _validate_offsets(value: Mapping[str, Any], field: str) -> None:
    start = value["start_offset"]
    end = value["end_offset"]
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or start < 0
        or isinstance(end, bool)
        or not isinstance(end, int)
        or end < start
    ):
        raise RuleSourceContractError(
            f"{field}.start_offset/end_offset must be ordered non-negative integers"
        )

def _required_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleSourceContractError(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise RuleSourceContractError(f"{field} exceeds {maximum} characters")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise RuleSourceContractError(f"{field} is missing: " + ", ".join(missing))
    if unknown:
        raise RuleSourceContractError(f"{field} has unsupported fields: " + ", ".join(unknown))
