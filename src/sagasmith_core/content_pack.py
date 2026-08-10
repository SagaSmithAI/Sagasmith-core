"""Unified, content-addressed SagaSmith content packages.

The package descriptor is system neutral.  Addons, modules, presets, and core
rules share one source/evidence/archive layer while retaining distinct content
and activation semantics.  Runtime database identifiers and campaign state are
never portable.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import zipfile
from typing import Any, Mapping, Sequence

from sagasmith_core.integrity import canonical_json

CONTENT_PACKAGE_FORMAT = "sagasmith.content-package"
CONTENT_PACKAGE_SCHEMA_VERSION = 2
ACTOR_CARD_SCHEMA = "sagasmith.actor-card.v3"
PACKAGE_KINDS = frozenset({"addon", "module", "preset", "core_rules"})
ARCHIVE_DESCRIPTOR = "package.sagasmith.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,299}$")
_IMAGE_MEDIA_TYPES = frozenset({"image/avif", "image/jpeg", "image/png", "image/webp"})
_REQUIRED_CONTENT_FIELDS = {
    "addon": frozenset(
        {
            "classification",
            "editions",
            "activation",
            "conflicts",
            "rule_definitions",
            "artifacts",
            "mechanics",
        }
    ),
    "core_rules": frozenset(
        {
            "classification",
            "editions",
            "activation",
            "conflicts",
            "rule_definitions",
            "artifacts",
            "mechanics",
        }
    ),
    "module": frozenset(
        {
            "classification",
            "compatibility",
            "play_profile",
            "continuity",
            "activation",
            "scene_atlas",
            "catalogs",
            "narrative",
        }
    ),
    "preset": frozenset({"activation"}),
}


class ContentPackageError(ValueError):
    """Raised when a unified content package is malformed or modified."""


def _text(value: Any, field: str, *, maximum: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ContentPackageError(f"{field} must be a non-empty string up to {maximum} characters")
    return value


def _exact(value: Mapping[str, Any], fields: set[str], field: str) -> None:
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown or missing:
        details = []
        if unknown:
            details.append("unsupported fields: " + ", ".join(unknown))
        if missing:
            details.append("missing fields: " + ", ".join(missing))
        raise ContentPackageError(f"{field} has " + "; ".join(details))


def _checksum(value: Mapping[str, Any]) -> str:
    unsigned = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def content_package_checksum(value: Mapping[str, Any]) -> str:
    """Return the canonical checksum of a descriptor without its checksum field."""

    return _checksum(value)


def blob_descriptor(
    *,
    asset_key: str,
    kind: str,
    name: str,
    media_type: str,
    content: bytes,
    license: str,
    attribution: str,
    source_refs: Sequence[Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one content-addressed asset descriptor."""

    digest = hashlib.sha256(content).hexdigest()
    return {
        "asset_key": asset_key,
        "kind": kind,
        "name": name,
        "media_type": media_type,
        "checksum": digest,
        "size": len(content),
        "blob_key": f"blobs/sha256/{digest}",
        "license": license,
        "attribution": attribution,
        "source_refs": copy.deepcopy(list(source_refs or [])),
        "metadata": copy.deepcopy(dict(metadata or {})),
    }


def source_ref(
    *,
    source_key: str,
    chunk_key: str,
    page: int | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Build the only citation shape accepted by unified packages."""

    return {
        "source_key": source_key,
        "chunk_key": chunk_key,
        "page": page,
        "note": note,
    }


def build_source_bundle(
    *,
    source_key: str,
    title: str,
    normalized_text: str,
    edition: str = "",
    locale: str = "",
    version: str = "",
    publication_id: str = "",
    authority: str = "",
    sections: Sequence[Mapping[str, Any]],
    asset_key: str | None = None,
    original_asset_keys: Sequence[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
    license: str = "private",
    attribution: str = "User supplied source",
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    """Build a source bundle and its single normalized-document blob.

    Section and chunk inputs contain offsets and structural metadata, never a
    second copy of their text.  Hashes are derived from the normalized document.
    """

    if not normalized_text:
        raise ContentPackageError("normalized source text must not be empty")
    normalized_bytes = normalized_text.encode("utf-8")
    key = (
        asset_key
        or f"source.{hashlib.sha256(source_key.encode('utf-8')).hexdigest()[:16]}.normalized"
    )
    asset = blob_descriptor(
        asset_key=key,
        kind="normalized_document",
        name=f"{source_key.rsplit('/', 1)[-1]}.md",
        media_type="text/markdown",
        content=normalized_bytes,
        license=license,
        attribution=attribution,
        metadata={"source_key": source_key},
    )
    normalized_sections: list[dict[str, Any]] = []
    for section_index, raw_section in enumerate(sections):
        section = copy.deepcopy(dict(raw_section))
        chunks = list(section.pop("chunks", []))
        start = int(section["start_offset"])
        end = int(section["end_offset"])
        if start < 0 or end < start or end > len(normalized_text):
            raise ContentPackageError(
                f"source section {section_index} offsets are outside the document"
            )
        section["content_hash"] = hashlib.sha256(
            normalized_text[start:end].encode("utf-8")
        ).hexdigest()
        normalized_chunks = []
        for chunk_index, raw_chunk in enumerate(chunks):
            chunk = copy.deepcopy(dict(raw_chunk))
            chunk_start = int(chunk["start_offset"])
            chunk_end = int(chunk["end_offset"])
            if chunk_start < 0 or chunk_end < chunk_start or chunk_end > len(normalized_text):
                raise ContentPackageError(
                    f"source section {section_index} chunk {chunk_index} offsets "
                    "are outside the document"
                )
            chunk_content = normalized_text[chunk_start:chunk_end]
            chunk_hash = hashlib.sha256(chunk_content.encode("utf-8")).hexdigest()
            chunk["content_hash"] = chunk_hash
            section_ordinal = int(section.get("ordinal", section_index))
            chunk_ordinal = int(chunk.get("ordinal", chunk_index))
            chunk.setdefault(
                "key",
                f"{source_key}/section-{section_ordinal}/"
                f"chunk-{chunk_ordinal}-{chunk_hash[:16]}",
            )
            normalized_chunks.append(chunk)
        section["chunks"] = normalized_chunks
        normalized_sections.append(section)
    source = {
        "source_key": source_key,
        "title": title,
        "edition": edition,
        "locale": locale,
        "version": version,
        "publication_id": publication_id,
        "authority": authority,
        "normalized_document_asset_key": key,
        "original_asset_keys": list(original_asset_keys or []),
        "sections": normalized_sections,
        "metadata": copy.deepcopy(dict(metadata or {})),
    }
    return source, asset, normalized_bytes


def build_actor_card(
    *,
    actor_id: str,
    version: str,
    system_id: str,
    actor_type: str,
    name: str,
    sheet: Mapping[str, Any],
    notes: Mapping[str, Any],
    player_name: str | None = None,
    summary: str = "",
    provenance: Mapping[str, Any] | None = None,
    bindings: Sequence[Mapping[str, Any]] | None = None,
    image_asset_key: str | None = None,
    image_alt: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a v3 actor card whose optional image is owned by the package."""

    return {
        "schema": ACTOR_CARD_SCHEMA,
        "id": actor_id,
        "version": version,
        "system_id": system_id,
        "actor_type": actor_type,
        "name": name,
        "player_name": player_name,
        "summary": summary,
        "sheet": copy.deepcopy(dict(sheet)),
        "notes": copy.deepcopy(dict(notes)),
        "provenance": copy.deepcopy(dict(provenance or {})),
        "bindings": copy.deepcopy(list(bindings or [])),
        "image": (
            {"asset_key": image_asset_key, "alt": image_alt}
            if image_asset_key is not None
            else None
        ),
        "metadata": copy.deepcopy(dict(metadata or {})),
    }


def build_content_package(
    *,
    kind: str,
    package_id: str,
    version: str,
    system_id: str,
    manifest: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]],
    assets: Sequence[Mapping[str, Any]],
    content_reviews: Sequence[Mapping[str, Any]],
    actors: Sequence[Mapping[str, Any]],
    content: Mapping[str, Any],
    dependencies: Sequence[Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one unified descriptor.  Binary blobs are supplied at archive time."""

    package_manifest = copy.deepcopy(dict(manifest))
    package_manifest.update(
        {"id": package_id, "version": version, "system_id": system_id}
    )
    descriptor = {
        "format": CONTENT_PACKAGE_FORMAT,
        "schema_version": CONTENT_PACKAGE_SCHEMA_VERSION,
        "kind": kind,
        "id": package_id,
        "version": version,
        "system_id": system_id,
        "manifest": package_manifest,
        "dependencies": copy.deepcopy(list(dependencies or [])),
        "sources": copy.deepcopy(list(sources)),
        "assets": copy.deepcopy(list(assets)),
        "content_reviews": copy.deepcopy(list(content_reviews)),
        "actors": copy.deepcopy(list(actors)),
        "content": copy.deepcopy(dict(content)),
        "metadata": copy.deepcopy(dict(metadata or {})),
    }
    descriptor["checksum"] = _checksum(descriptor)
    return validate_content_package(descriptor)


def _validate_source_ref(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContentPackageError(f"{field} must be an object")
    _exact(value, {"source_key", "chunk_key", "page", "note"}, field)
    _text(value["source_key"], f"{field}.source_key", maximum=300)
    _text(value["chunk_key"], f"{field}.chunk_key", maximum=600)
    page = value["page"]
    if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 1):
        raise ContentPackageError(f"{field}.page must be null or positive")
    if not isinstance(value["note"], str):
        raise ContentPackageError(f"{field}.note must be a string")
    return value


def _validate_asset(value: Any, index: int) -> dict[str, Any]:
    field = f"content_package.assets[{index}]"
    if not isinstance(value, dict):
        raise ContentPackageError(f"{field} must be an object")
    _exact(
        value,
        {
            "asset_key",
            "kind",
            "name",
            "media_type",
            "checksum",
            "size",
            "blob_key",
            "license",
            "attribution",
            "source_refs",
            "metadata",
        },
        field,
    )
    _text(value["asset_key"], f"{field}.asset_key", maximum=300)
    _text(value["kind"], f"{field}.kind", maximum=100)
    _text(value["name"], f"{field}.name", maximum=500)
    _text(value["media_type"], f"{field}.media_type", maximum=100)
    checksum = _text(value["checksum"], f"{field}.checksum", maximum=64)
    if not _SHA256_RE.fullmatch(checksum):
        raise ContentPackageError(f"{field}.checksum must be a lowercase SHA-256")
    if value["blob_key"] != f"blobs/sha256/{checksum}":
        raise ContentPackageError(f"{field}.blob_key does not match checksum")
    if isinstance(value["size"], bool) or not isinstance(value["size"], int) or value["size"] < 1:
        raise ContentPackageError(f"{field}.size must be positive")
    _text(value["license"], f"{field}.license", maximum=300)
    _text(value["attribution"], f"{field}.attribution", maximum=3000)
    if not isinstance(value["source_refs"], list):
        raise ContentPackageError(f"{field}.source_refs must be an array")
    for ref_index, ref in enumerate(value["source_refs"]):
        _validate_source_ref(ref, f"{field}.source_refs[{ref_index}]")
    if not isinstance(value["metadata"], dict):
        raise ContentPackageError(f"{field}.metadata must be an object")
    return value


def validate_actor_card(
    value: Mapping[str, Any],
    *,
    expected_system_id: str | None = None,
    assets_by_key: Mapping[str, Mapping[str, Any]] | None = None,
    field: str = "actor",
) -> dict[str, Any]:
    """Validate the only supported actor-card schema.

    Detached cards may omit an image. Cards with an image must be validated
    with their owning package's asset map so the reference cannot dangle.
    """

    if not isinstance(value, Mapping):
        raise ContentPackageError(f"{field} must be an object")
    value = copy.deepcopy(dict(value))
    assets_by_key = assets_by_key or {}
    _exact(
        value,
        {
            "schema",
            "id",
            "version",
            "system_id",
            "actor_type",
            "name",
            "player_name",
            "summary",
            "sheet",
            "notes",
            "provenance",
            "bindings",
            "image",
            "metadata",
        },
        field,
    )
    if value["schema"] != ACTOR_CARD_SCHEMA:
        raise ContentPackageError(f"{field}.schema must be {ACTOR_CARD_SCHEMA!r}")
    actor_id = _text(value["id"], f"{field}.id", maximum=300)
    if not _ID_RE.fullmatch(actor_id):
        raise ContentPackageError(f"{field}.id is not portable")
    _text(value["version"], f"{field}.version", maximum=100)
    if expected_system_id is not None and value["system_id"] != expected_system_id:
        raise ContentPackageError(f"{field}.system_id does not match expected system")
    _text(value["actor_type"], f"{field}.actor_type", maximum=100)
    _text(value["name"], f"{field}.name", maximum=300)
    if value["player_name"] is not None:
        _text(value["player_name"], f"{field}.player_name", maximum=300)
    if not isinstance(value["summary"], str):
        raise ContentPackageError(f"{field}.summary must be a string")
    for name in ("sheet", "notes", "provenance", "metadata"):
        if not isinstance(value[name], dict):
            raise ContentPackageError(f"{field}.{name} must be an object")
    if not isinstance(value["bindings"], list):
        raise ContentPackageError(f"{field}.bindings must be an array")
    image = value["image"]
    if image is not None:
        if not isinstance(image, dict):
            raise ContentPackageError(f"{field}.image must be an object or null")
        _exact(image, {"asset_key", "alt"}, f"{field}.image")
        if image["asset_key"] not in assets_by_key:
            raise ContentPackageError(f"{field}.image references an unknown asset")
        if assets_by_key[image["asset_key"]]["kind"] != "actor_image":
            raise ContentPackageError(f"{field}.image must reference an actor_image asset")
        if not isinstance(image["alt"], str):
            raise ContentPackageError(f"{field}.image.alt must be a string")
    return value


def _validate_actor(
    value: Any,
    index: int,
    *,
    system_id: str,
    assets_by_key: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return validate_actor_card(
        value,
        expected_system_id=system_id,
        assets_by_key=assets_by_key,
        field=f"content_package.actors[{index}]",
    )


def validate_content_package(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a descriptor and every stable cross-reference."""

    if not isinstance(value, Mapping):
        raise ContentPackageError("content package must be an object")
    result = copy.deepcopy(dict(value))
    _exact(
        result,
        {
            "format",
            "schema_version",
            "kind",
            "id",
            "version",
            "system_id",
            "manifest",
            "dependencies",
            "sources",
            "assets",
            "content_reviews",
            "actors",
            "content",
            "metadata",
            "checksum",
        },
        "content package",
    )
    if (
        result["format"] != CONTENT_PACKAGE_FORMAT
        or result["schema_version"] != CONTENT_PACKAGE_SCHEMA_VERSION
    ):
        raise ContentPackageError("unsupported content package format or schema version")
    if result["kind"] not in PACKAGE_KINDS:
        raise ContentPackageError("content_package.kind is unsupported")
    package_id = _text(result["id"], "content_package.id", maximum=300)
    if not _ID_RE.fullmatch(package_id):
        raise ContentPackageError("content_package.id is not portable")
    _text(result["version"], "content_package.version", maximum=100)
    _text(result["system_id"], "content_package.system_id", maximum=100)
    for name in ("manifest", "content", "metadata"):
        if not isinstance(result[name], dict):
            raise ContentPackageError(f"content_package.{name} must be an object")
    for field, expected in (
        ("id", result["id"]),
        ("version", result["version"]),
        ("system_id", result["system_id"]),
    ):
        if result["manifest"].get(field) != expected:
            raise ContentPackageError(
                f"content_package.manifest.{field} must match the package identity"
            )
    for name in ("dependencies", "sources", "assets", "content_reviews", "actors"):
        if not isinstance(result[name], list):
            raise ContentPackageError(f"content_package.{name} must be an array")
    for index, dependency in enumerate(result["dependencies"]):
        field = f"content_package.dependencies[{index}]"
        if not isinstance(dependency, dict):
            raise ContentPackageError(f"{field} must be an object")
        _exact(dependency, {"kind", "id", "version", "checksum", "optional"}, field)
        if dependency["kind"] not in PACKAGE_KINDS:
            raise ContentPackageError(f"{field}.kind is unsupported")
        _text(dependency["id"], f"{field}.id", maximum=300)
        _text(dependency["version"], f"{field}.version", maximum=100)
        if not isinstance(dependency["optional"], bool):
            raise ContentPackageError(f"{field}.optional must be a boolean")
        if not _SHA256_RE.fullmatch(str(dependency["checksum"])):
            raise ContentPackageError(f"{field}.checksum must be a lowercase SHA-256")
    asset_keys: set[str] = set()
    assets_by_key: dict[str, dict[str, Any]] = {}
    checksum_assets: dict[str, dict[str, Any]] = {}
    for index, asset in enumerate(result["assets"]):
        normalized = _validate_asset(asset, index)
        if normalized["asset_key"] in asset_keys:
            raise ContentPackageError(f"duplicate asset_key: {normalized['asset_key']}")
        asset_keys.add(normalized["asset_key"])
        assets_by_key[normalized["asset_key"]] = normalized
        checksum_assets.setdefault(normalized["checksum"], normalized)
        if (
            normalized["kind"] == "actor_image"
            and normalized["media_type"] not in _IMAGE_MEDIA_TYPES
        ):
            raise ContentPackageError(
                f"content_package.assets[{index}] actor images require "
                "a supported image media type"
            )
    source_keys: set[str] = set()
    chunk_owners: dict[str, str] = {}
    normalized_asset_keys: set[str] = set()
    for source_index, source in enumerate(result["sources"]):
        field = f"content_package.sources[{source_index}]"
        if not isinstance(source, dict):
            raise ContentPackageError(f"{field} must be an object")
        _exact(
            source,
            {
                "source_key",
                "title",
                "edition",
                "locale",
                "version",
                "publication_id",
                "authority",
                "normalized_document_asset_key",
                "original_asset_keys",
                "sections",
                "metadata",
            },
            field,
        )
        source_key = _text(source["source_key"], f"{field}.source_key", maximum=300)
        if source_key in source_keys:
            raise ContentPackageError(f"duplicate source_key: {source_key}")
        source_keys.add(source_key)
        _text(source["title"], f"{field}.title", maximum=500)
        for name in ("edition", "locale", "version", "publication_id", "authority"):
            if not isinstance(source[name], str):
                raise ContentPackageError(f"{field}.{name} must be a string")
        normalized_key = source["normalized_document_asset_key"]
        if normalized_key not in asset_keys:
            raise ContentPackageError(f"{field}.normalized_document_asset_key is unknown")
        normalized_asset_keys.add(normalized_key)
        if not isinstance(source["original_asset_keys"], list) or any(
            key not in asset_keys for key in source["original_asset_keys"]
        ):
            raise ContentPackageError(f"{field}.original_asset_keys contains an unknown asset")
        if (
            not isinstance(source["metadata"], dict)
            or not isinstance(source["sections"], list)
            or not source["sections"]
        ):
            raise ContentPackageError(f"{field} requires metadata and non-empty sections")
        section_ordinals: set[int] = set()
        for section_index, section in enumerate(source["sections"]):
            section_field = f"{field}.sections[{section_index}]"
            if not isinstance(section, dict):
                raise ContentPackageError(f"{section_field} must be an object")
            _exact(
                section,
                {
                    "ordinal",
                    "parent_ordinal",
                    "level",
                    "title",
                    "path",
                    "start_offset",
                    "end_offset",
                    "content_hash",
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
                raise ContentPackageError(f"{section_field}.ordinal is invalid or duplicated")
            section_ordinals.add(ordinal)
            for offset in ("start_offset", "end_offset"):
                if (
                    isinstance(section[offset], bool)
                    or not isinstance(section[offset], int)
                    or section[offset] < 0
                ):
                    raise ContentPackageError(f"{section_field}.{offset} is invalid")
            if section["end_offset"] < section["start_offset"] or not _SHA256_RE.fullmatch(
                str(section["content_hash"])
            ):
                raise ContentPackageError(f"{section_field} range or hash is invalid")
            if (
                not isinstance(section["path"], list)
                or not isinstance(section["chunks"], list)
                or not section["chunks"]
            ):
                raise ContentPackageError(f"{section_field} requires path and chunks")
            for chunk_index, chunk in enumerate(section["chunks"]):
                chunk_field = f"{section_field}.chunks[{chunk_index}]"
                if not isinstance(chunk, dict):
                    raise ContentPackageError(f"{chunk_field} must be an object")
                _exact(
                    chunk,
                    {
                        "key",
                        "ordinal",
                        "heading_path",
                        "start_offset",
                        "end_offset",
                        "content_hash",
                        "token_count",
                        "page_start",
                        "page_end",
                        "metadata",
                    },
                    chunk_field,
                )
                chunk_key = _text(chunk["key"], f"{chunk_field}.key", maximum=600)
                if chunk_key in chunk_owners:
                    raise ContentPackageError(f"duplicate chunk key: {chunk_key}")
                chunk_owners[chunk_key] = source_key
                if (
                    isinstance(chunk["start_offset"], bool)
                    or not isinstance(chunk["start_offset"], int)
                    or isinstance(chunk["end_offset"], bool)
                    or not isinstance(chunk["end_offset"], int)
                    or chunk["start_offset"] < 0
                    or chunk["end_offset"] < chunk["start_offset"]
                    or not _SHA256_RE.fullmatch(str(chunk["content_hash"]))
                ):
                    raise ContentPackageError(f"{chunk_field} range or hash is invalid")
                for page_name in ("page_start", "page_end"):
                    page = chunk[page_name]
                    if page is not None and (
                        isinstance(page, bool) or not isinstance(page, int) or page < 1
                    ):
                        raise ContentPackageError(f"{chunk_field}.{page_name} is invalid")
                if (
                    chunk["page_start"] is not None
                    and chunk["page_end"] is not None
                    and chunk["page_end"] < chunk["page_start"]
                ):
                    raise ContentPackageError(f"{chunk_field} page range is invalid")
                if not isinstance(chunk["heading_path"], list) or not isinstance(
                    chunk["metadata"], dict
                ):
                    raise ContentPackageError(f"{chunk_field} headings or metadata is invalid")
    actor_ids: set[str] = set()
    for index, actor in enumerate(result["actors"]):
        normalized_actor = _validate_actor(
            actor,
            index,
            system_id=result["system_id"],
            assets_by_key=assets_by_key,
        )
        if normalized_actor["id"] in actor_ids:
            raise ContentPackageError(f"duplicate actor id: {normalized_actor['id']}")
        actor_ids.add(normalized_actor["id"])
    for index, review in enumerate(result["content_reviews"]):
        field = f"content_package.content_reviews[{index}]"
        if not isinstance(review, dict):
            raise ContentPackageError(f"{field} must be an object")
        for ref_index, ref in enumerate(review.get("source_refs", [])):
            _validate_source_ref(ref, f"{field}.source_refs[{ref_index}]")

    def inspect_refs(item: Any, field: str) -> None:
        if isinstance(item, dict):
            if set(item) == {"source_key", "chunk_key", "page", "note"}:
                ref = _validate_source_ref(item, field)
                if chunk_owners.get(ref["chunk_key"]) != ref["source_key"]:
                    raise ContentPackageError(f"{field} does not resolve inside packaged sources")
                return
            for key, child in item.items():
                inspect_refs(child, f"{field}.{key}")
        elif isinstance(item, list):
            for child_index, child in enumerate(item):
                inspect_refs(child, f"{field}[{child_index}]")

    inspect_refs(result["manifest"], "content_package.manifest")
    inspect_refs(result["assets"], "content_package.assets")
    inspect_refs(result["content"], "content_package.content")
    inspect_refs(result["content_reviews"], "content_package.content_reviews")
    inspect_refs(result["actors"], "content_package.actors")
    missing_content_fields = sorted(
        _REQUIRED_CONTENT_FIELDS[result["kind"]] - set(result["content"])
    )
    if missing_content_fields:
        raise ContentPackageError(
            "content_package.content is missing fields for "
            f"{result['kind']}: {', '.join(missing_content_fields)}"
        )
    if result["kind"] in {"addon", "core_rules"} and not result["sources"]:
        raise ContentPackageError("rule-bearing packages require at least one source")
    if result["kind"] == "module" and not result["content"].get("scene_atlas"):
        raise ContentPackageError("module packages require a non-empty scene_atlas")
    if result["kind"] == "preset" and not result["actors"]:
        raise ContentPackageError("preset packages require at least one actor")
    checksum = str(result["checksum"])
    if not _SHA256_RE.fullmatch(checksum) or checksum != _checksum(result):
        raise ContentPackageError("content package checksum mismatch")
    return result


def dumps_content_archive(package: Mapping[str, Any], blobs: Mapping[str, bytes]) -> bytes:
    """Serialize any content package using the same deterministic ZIP layout."""

    value = validate_content_package(package)
    expected = {asset["checksum"]: asset for asset in value["assets"]}
    normalized = {
        str(key).removeprefix("blobs/sha256/"): bytes(data) for key, data in blobs.items()
    }
    if set(normalized) != set(expected):
        raise ContentPackageError("archive blobs do not match asset descriptors")
    for checksum, content in normalized.items():
        asset = expected[checksum]
        if len(content) != asset["size"] or hashlib.sha256(content).hexdigest() != checksum:
            raise ContentPackageError(f"archive blob mismatch: {checksum}")
    output = io.BytesIO()

    def archive_info(name: str, compress_type: int) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = compress_type
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        return info

    with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
        archive.writestr(
            archive_info(ARCHIVE_DESCRIPTOR, zipfile.ZIP_DEFLATED),
            canonical_json(value).encode("utf-8"),
        )
        for checksum, content in sorted(normalized.items()):
            archive.writestr(
                archive_info(f"blobs/sha256/{checksum}", zipfile.ZIP_STORED),
                content,
            )
    return output.getvalue()


def loads_content_archive(
    content: bytes, *, maximum_uncompressed_bytes: int = 4 * 1024 * 1024 * 1024
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Load and fully verify any unified content archive."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(content), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ContentPackageError("invalid content package archive") from exc
    with archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(infos) > 100_000:
            raise ContentPackageError("archive contains too many entries")
        if len(names) != len(set(names)) or ARCHIVE_DESCRIPTOR not in names:
            raise ContentPackageError("archive descriptor is missing or paths are duplicated")
        if any(item.flag_bits & 0x1 for item in infos):
            raise ContentPackageError("encrypted archive entries are unsupported")
        if sum(item.file_size for item in infos) > maximum_uncompressed_bytes:
            raise ContentPackageError("archive exceeds the uncompressed size limit")
        if any(
            item.file_size > 64 * 1024 * 1024
            and item.compress_size > 0
            and item.file_size / item.compress_size > 200
            for item in infos
        ):
            raise ContentPackageError("archive contains an unsafe compression ratio")
        allowed_blob = re.compile(r"blobs/sha256/[0-9a-f]{64}")
        if any(name != ARCHIVE_DESCRIPTOR and not allowed_blob.fullmatch(name) for name in names):
            raise ContentPackageError("archive contains unsupported paths")
        try:
            package = validate_content_package(
                json.loads(archive.read(ARCHIVE_DESCRIPTOR).decode("utf-8"))
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContentPackageError("archive descriptor is invalid") from exc
        blobs = {
            name.removeprefix("blobs/sha256/"): archive.read(name)
            for name in names
            if name.startswith("blobs/sha256/")
        }
    expected = {asset["checksum"]: asset for asset in package["assets"]}
    if set(blobs) != set(expected):
        raise ContentPackageError("archive blobs do not match asset descriptors")
    for checksum, data in blobs.items():
        asset = expected[checksum]
        if len(data) != asset["size"] or hashlib.sha256(data).hexdigest() != checksum:
            raise ContentPackageError(f"archive blob mismatch: {checksum}")
    for source in package["sources"]:
        asset = next(
            item
            for item in package["assets"]
            if item["asset_key"] == source["normalized_document_asset_key"]
        )
        if asset["media_type"] not in {"text/markdown", "text/plain"}:
            raise ContentPackageError("normalized source document must be Markdown or plain text")
        try:
            document = blobs[asset["checksum"]].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContentPackageError("normalized source document must be UTF-8") from exc
        for section in source["sections"]:
            if (
                section["end_offset"] > len(document)
                or hashlib.sha256(
                    document[section["start_offset"] : section["end_offset"]].encode("utf-8")
                ).hexdigest()
                != section["content_hash"]
            ):
                raise ContentPackageError("source section does not match normalized document")
            for chunk in section["chunks"]:
                if (
                    chunk["end_offset"] > len(document)
                    or hashlib.sha256(
                        document[chunk["start_offset"] : chunk["end_offset"]].encode("utf-8")
                    ).hexdigest()
                    != chunk["content_hash"]
                ):
                    raise ContentPackageError("source chunk does not match normalized document")
    return package, blobs
