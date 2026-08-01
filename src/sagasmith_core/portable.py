"""Versioned, content-addressed interchange envelopes for SagaSmith content.

Portable packages deliberately exclude runtime database identifiers, revisions,
campaign state, and actor knowledge.  A receiving runtime creates fresh local
identities and resolves logical bindings (for example a scene stable key) at
import time.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

PORTABLE_FORMAT = "sagasmith.portable"
PORTABLE_SCHEMA_VERSION = 1
PORTABLE_KINDS = frozenset({"actor_card", "module_pack", "preset_pack"})
ACTOR_CARD_SCHEMA = "sagasmith.actor-card.v1"
MODULE_PACK_SCHEMA = "sagasmith.module-pack.v1"
PRESET_PACK_SCHEMA = "sagasmith.preset-pack.v1"

_PORTABLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,199}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PortableContentError(ValueError):
    """Raised when a portable envelope is malformed or has been modified."""


def canonical_json(value: Any) -> str:
    """Serialize portable content deterministically for hashing and sharing."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def portable_checksum(value: Mapping[str, Any]) -> str:
    """Return the SHA-256 of an envelope without its top-level checksum."""

    unsigned = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def build_portable_envelope(
    *,
    kind: str,
    portable_id: str,
    version: str,
    system_id: str,
    payload: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
    dependencies: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build and validate one immutable portable content envelope."""

    value = {
        "format": PORTABLE_FORMAT,
        "schema_version": PORTABLE_SCHEMA_VERSION,
        "kind": kind,
        "id": portable_id,
        "version": version,
        "system_id": system_id,
        "metadata": copy.deepcopy(dict(metadata or {})),
        "dependencies": copy.deepcopy(list(dependencies or [])),
        "payload": copy.deepcopy(dict(payload)),
    }
    value["checksum"] = portable_checksum(value)
    return validate_portable_envelope(value, expected_kind=kind)


def validate_portable_envelope(
    envelope: Mapping[str, Any],
    *,
    expected_kind: str | None = None,
    verify_checksum: bool = True,
) -> dict[str, Any]:
    """Strictly validate an envelope and return a detached normalized copy."""

    if not isinstance(envelope, Mapping):
        raise PortableContentError("portable content must be an object")
    value = copy.deepcopy(dict(envelope))
    allowed = {
        "format",
        "schema_version",
        "kind",
        "id",
        "version",
        "system_id",
        "metadata",
        "dependencies",
        "payload",
        "checksum",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PortableContentError(
            "portable content has unsupported fields: " + ", ".join(unknown)
        )
    required = allowed
    missing = sorted(required - set(value))
    if missing:
        raise PortableContentError("portable content is missing: " + ", ".join(missing))
    if value["format"] != PORTABLE_FORMAT:
        raise PortableContentError(f"portable format must be {PORTABLE_FORMAT!r}")
    if value["schema_version"] != PORTABLE_SCHEMA_VERSION:
        raise PortableContentError(
            f"portable schema_version must be {PORTABLE_SCHEMA_VERSION}"
        )
    kind = _required_text(value["kind"], "portable.kind", maximum=50)
    if kind not in PORTABLE_KINDS:
        raise PortableContentError(f"unsupported portable kind: {kind}")
    if expected_kind is not None and kind != expected_kind:
        raise PortableContentError(
            f"portable kind must be {expected_kind!r}, received {kind!r}"
        )
    portable_id = _required_text(value["id"], "portable.id", maximum=200)
    if not _PORTABLE_ID_RE.fullmatch(portable_id):
        raise PortableContentError(
            "portable.id must start with a lowercase letter or digit and contain only "
            "lowercase letters, digits, '.', '_', '/', or '-'"
        )
    _required_text(value["version"], "portable.version", maximum=100)
    _required_text(value["system_id"], "portable.system_id", maximum=64)
    if not isinstance(value["metadata"], dict):
        raise PortableContentError("portable.metadata must be an object")
    if not isinstance(value["dependencies"], list):
        raise PortableContentError("portable.dependencies must be an array")
    value["dependencies"] = [
        _validate_dependency(item, index)
        for index, item in enumerate(value["dependencies"])
    ]
    if not isinstance(value["payload"], dict):
        raise PortableContentError("portable.payload must be an object")
    checksum = _required_text(value["checksum"], "portable.checksum", maximum=64)
    if not _SHA256_RE.fullmatch(checksum):
        raise PortableContentError("portable.checksum must be a lowercase SHA-256")
    if verify_checksum:
        expected = portable_checksum(value)
        if checksum != expected:
            raise PortableContentError(
                f"portable checksum mismatch: expected {expected}, received {checksum}"
            )
    return value


def build_actor_card(
    *,
    portable_id: str,
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
    metadata: Mapping[str, Any] | None = None,
    dependencies: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a system-neutral actor card around a system-owned sheet."""

    payload = {
        "card_schema": ACTOR_CARD_SCHEMA,
        "actor_type": actor_type,
        "name": name,
        "player_name": player_name,
        "summary": summary,
        "sheet": copy.deepcopy(dict(sheet)),
        "notes": copy.deepcopy(dict(notes)),
        "provenance": copy.deepcopy(dict(provenance or {})),
        "bindings": copy.deepcopy(list(bindings or [])),
    }
    envelope = build_portable_envelope(
        kind="actor_card",
        portable_id=portable_id,
        version=version,
        system_id=system_id,
        payload=payload,
        metadata=metadata,
        dependencies=dependencies,
    )
    return validate_actor_card(envelope)


def validate_actor_card(
    envelope: Mapping[str, Any], *, expected_system_id: str | None = None
) -> dict[str, Any]:
    """Validate the shared portion of an actor card.

    The system plugin remains responsible for validating ``sheet``, ``notes``,
    and the system-specific actor-type vocabulary.
    """

    value = validate_portable_envelope(envelope, expected_kind="actor_card")
    if expected_system_id is not None and value["system_id"] != expected_system_id:
        raise PortableContentError(
            f"actor card system_id must be {expected_system_id!r}"
        )
    payload = value["payload"]
    allowed = {
        "card_schema",
        "actor_type",
        "name",
        "player_name",
        "summary",
        "sheet",
        "notes",
        "provenance",
        "bindings",
    }
    _exact_fields(payload, allowed, "actor card payload")
    if payload["card_schema"] != ACTOR_CARD_SCHEMA:
        raise PortableContentError(f"card_schema must be {ACTOR_CARD_SCHEMA!r}")
    _required_text(payload["actor_type"], "actor_card.actor_type", maximum=50)
    _required_text(payload["name"], "actor_card.name", maximum=300)
    player_name = payload["player_name"]
    if player_name is not None:
        _required_text(player_name, "actor_card.player_name", maximum=300)
    if not isinstance(payload["summary"], str) or len(payload["summary"]) > 4000:
        raise PortableContentError("actor_card.summary must be a string up to 4000 characters")
    for field in ("sheet", "notes", "provenance"):
        if not isinstance(payload[field], dict):
            raise PortableContentError(f"actor_card.{field} must be an object")
    if not isinstance(payload["bindings"], list):
        raise PortableContentError("actor_card.bindings must be an array")
    for index, binding in enumerate(payload["bindings"]):
        if not isinstance(binding, dict):
            raise PortableContentError(f"actor_card.bindings[{index}] must be an object")
        binding_kind = _required_text(
            binding.get("kind"), f"actor_card.bindings[{index}].kind", maximum=100
        )
        if binding_kind not in {"module", "module_scene"}:
            raise PortableContentError(
                f"actor_card.bindings[{index}].kind is unsupported: {binding_kind}"
            )
        allowed_binding = {
            "kind",
            "module_key",
            "binding_kind",
            "role",
            "metadata",
            *({"scene_key"} if binding_kind == "module_scene" else set()),
        }
        unknown_binding = sorted(set(binding) - allowed_binding)
        if unknown_binding:
            raise PortableContentError(
                f"actor_card.bindings[{index}] has unsupported fields: "
                + ", ".join(unknown_binding)
            )
        if binding_kind in {"module", "module_scene"}:
            _required_text(
                binding.get("module_key"),
                f"actor_card.bindings[{index}].module_key",
                maximum=200,
            )
        if binding_kind == "module_scene":
            _required_text(
                binding.get("scene_key"),
                f"actor_card.bindings[{index}].scene_key",
                maximum=300,
            )
        semantic_kind = binding.get("binding_kind")
        if semantic_kind is not None:
            normalized_semantic_kind = _required_text(
                semantic_kind,
                f"actor_card.bindings[{index}].binding_kind",
                maximum=50,
            )
            if normalized_semantic_kind not in {"cast", "encounter", "preset_pc"}:
                raise PortableContentError(
                    f"actor_card.bindings[{index}].binding_kind must be cast, "
                    "encounter, or preset_pc"
                )
        role = binding.get("role")
        if role is not None and (not isinstance(role, str) or len(role) > 200):
            raise PortableContentError(
                f"actor_card.bindings[{index}].role must be a string up to 200 characters"
            )
        binding_metadata = binding.get("metadata")
        if binding_metadata is not None and not isinstance(binding_metadata, dict):
            raise PortableContentError(
                f"actor_card.bindings[{index}].metadata must be an object"
            )
    return value


def build_module_pack(
    *,
    portable_id: str,
    version: str,
    system_id: str,
    source: Mapping[str, Any],
    document: Mapping[str, Any],
    scene_atlas: Sequence[Mapping[str, Any]],
    assets: Sequence[Mapping[str, Any]] | None = None,
    content_reviews: Sequence[Mapping[str, Any]] | None = None,
    actors: Sequence[Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
    dependencies: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a self-contained adventure-module package."""

    envelope = build_portable_envelope(
        kind="module_pack",
        portable_id=portable_id,
        version=version,
        system_id=system_id,
        payload={
            "module_schema": MODULE_PACK_SCHEMA,
            "source": copy.deepcopy(dict(source)),
            "document": copy.deepcopy(dict(document)),
            "scene_atlas": copy.deepcopy(list(scene_atlas)),
            "assets": copy.deepcopy(list(assets or [])),
            "content_reviews": copy.deepcopy(list(content_reviews or [])),
            "actors": copy.deepcopy(list(actors or [])),
        },
        metadata=metadata,
        dependencies=dependencies,
    )
    return validate_module_pack(envelope)


def validate_module_pack(
    envelope: Mapping[str, Any], *, expected_system_id: str | None = None
) -> dict[str, Any]:
    """Validate module content, stable references, embedded files, and actor cards."""

    value = validate_portable_envelope(envelope, expected_kind="module_pack")
    if expected_system_id is not None and value["system_id"] != expected_system_id:
        raise PortableContentError(
            f"module pack system_id must be {expected_system_id!r}"
        )
    payload = value["payload"]
    allowed = {
        "module_schema",
        "source",
        "document",
        "scene_atlas",
        "assets",
        "content_reviews",
        "actors",
    }
    _exact_fields(payload, allowed, "module pack payload")
    if payload["module_schema"] != MODULE_PACK_SCHEMA:
        raise PortableContentError(f"module_schema must be {MODULE_PACK_SCHEMA!r}")
    source = payload["source"]
    if not isinstance(source, dict):
        raise PortableContentError("module_pack.source must be an object")
    _required_text(source.get("source_key"), "module_pack.source.source_key", maximum=200)
    _required_text(source.get("title"), "module_pack.source.title", maximum=300)
    for field in ("parser_profile", "parser_version"):
        _required_text(source.get(field), f"module_pack.source.{field}", maximum=100)
    if not isinstance(source.get("metadata", {}), dict):
        raise PortableContentError("module_pack.source.metadata must be an object")

    document = payload["document"]
    if not isinstance(document, dict):
        raise PortableContentError("module_pack.document must be an object")
    _exact_fields(document, {"media_type", "content", "checksum"}, "module document")
    media_type = _required_text(
        document["media_type"], "module_pack.document.media_type", maximum=100
    )
    if media_type not in {"text/markdown", "text/plain"}:
        raise PortableContentError("portable module document must be Markdown or plain text")
    if not isinstance(document["content"], str) or not document["content"].strip():
        raise PortableContentError("module_pack.document.content must not be empty")
    document_checksum = hashlib.sha256(document["content"].encode("utf-8")).hexdigest()
    if document["checksum"] != document_checksum:
        raise PortableContentError("module document checksum mismatch")

    scene_atlas = payload["scene_atlas"]
    if not isinstance(scene_atlas, list) or not scene_atlas:
        raise PortableContentError("module_pack.scene_atlas must be a non-empty array")
    scene_keys: set[str] = set()
    scene_ordinals: set[tuple[int, int]] = set()
    chapter_titles: dict[int, str] = {}
    for index, raw_scene in enumerate(scene_atlas):
        field = f"module_pack.scene_atlas[{index}]"
        if not isinstance(raw_scene, dict):
            raise PortableContentError(f"{field} must be an object")
        key = _required_text(raw_scene.get("stable_key"), f"{field}.stable_key", maximum=300)
        if key in scene_keys:
            raise PortableContentError(f"duplicate module scene stable_key: {key}")
        scene_keys.add(key)
        _required_text(raw_scene.get("title"), f"{field}.title", maximum=500)
        ordinals: dict[str, int] = {}
        for ordinal_field in ("chapter_ordinal", "scene_ordinal"):
            ordinal = raw_scene.get(ordinal_field)
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                raise PortableContentError(
                    f"{field}.{ordinal_field} must be a non-negative integer"
                )
            ordinals[ordinal_field] = ordinal
        ordinal_key = (ordinals["chapter_ordinal"], ordinals["scene_ordinal"])
        if ordinal_key in scene_ordinals:
            raise PortableContentError(
                f"duplicate module scene ordinal: {ordinal_key[0]}/{ordinal_key[1]}"
            )
        scene_ordinals.add(ordinal_key)
        chapter = _required_text(raw_scene.get("chapter"), f"{field}.chapter", maximum=500)
        existing_chapter = chapter_titles.setdefault(ordinal_key[0], chapter)
        if existing_chapter != chapter:
            raise PortableContentError(
                f"{field}.chapter conflicts with another scene in chapter {ordinal_key[0]}"
            )
        _required_text(raw_scene.get("scene_type"), f"{field}.scene_type", maximum=100)
        for page_field in ("page_start", "page_end"):
            page = raw_scene.get(page_field)
            if page is not None and (
                isinstance(page, bool) or not isinstance(page, int) or page < 1
            ):
                raise PortableContentError(
                    f"{field}.{page_field} must be null or a positive integer"
                )
        for list_field in ("headings", "keywords"):
            items = raw_scene.get(list_field)
            if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                raise PortableContentError(f"{field}.{list_field} must be a string array")
        if not isinstance(raw_scene.get("metadata"), dict):
            raise PortableContentError(f"{field}.metadata must be an object")
        content = raw_scene.get("content")
        if not isinstance(content, str) or not content.strip():
            raise PortableContentError(f"{field}.content must not be empty")
        content_checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if raw_scene.get("content_checksum") != content_checksum:
            raise PortableContentError(f"{field}.content_checksum does not match content")
        chunks = raw_scene.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise PortableContentError(f"{field}.chunks must be a non-empty array")
        chunk_ordinals: set[int] = set()
        for chunk_index, raw_chunk in enumerate(chunks):
            chunk_field = f"{field}.chunks[{chunk_index}]"
            if not isinstance(raw_chunk, dict):
                raise PortableContentError(f"{chunk_field} must be an object")
            _exact_fields(
                raw_chunk,
                {
                    "ordinal",
                    "heading_path",
                    "content",
                    "start_offset",
                    "end_offset",
                    "metadata",
                    "content_hash",
                },
                chunk_field,
            )
            chunk_ordinal = raw_chunk["ordinal"]
            if (
                isinstance(chunk_ordinal, bool)
                or not isinstance(chunk_ordinal, int)
                or chunk_ordinal < 0
                or chunk_ordinal in chunk_ordinals
            ):
                raise PortableContentError(
                    f"{chunk_field}.ordinal must be a unique non-negative integer"
                )
            chunk_ordinals.add(chunk_ordinal)
            headings = raw_chunk["heading_path"]
            if not isinstance(headings, list) or any(
                not isinstance(heading, str) or not heading.strip() for heading in headings
            ):
                raise PortableContentError(
                    f"{chunk_field}.heading_path must be an array of non-empty strings"
                )
            chunk_content = raw_chunk["content"]
            if not isinstance(chunk_content, str):
                raise PortableContentError(f"{chunk_field}.content must be a string")
            for offset_field in ("start_offset", "end_offset"):
                offset = raw_chunk[offset_field]
                if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                    raise PortableContentError(
                        f"{chunk_field}.{offset_field} must be a non-negative integer"
                    )
            if raw_chunk["end_offset"] < raw_chunk["start_offset"]:
                raise PortableContentError(
                    f"{chunk_field}.end_offset must not precede start_offset"
                )
            if not isinstance(raw_chunk["metadata"], dict):
                raise PortableContentError(f"{chunk_field}.metadata must be an object")
            chunk_hash = hashlib.sha256(chunk_content.encode("utf-8")).hexdigest()
            if raw_chunk["content_hash"] != chunk_hash:
                raise PortableContentError(
                    f"{chunk_field}.content_hash does not match content"
                )

    assets = payload["assets"]
    if not isinstance(assets, list):
        raise PortableContentError("module_pack.assets must be an array")
    asset_keys: set[str] = set()
    for index, raw_asset in enumerate(assets):
        field = f"module_pack.assets[{index}]"
        if not isinstance(raw_asset, dict):
            raise PortableContentError(f"{field} must be an object")
        allowed_asset = {
            "asset_key",
            "name",
            "media_type",
            "checksum",
            "size",
            "data_base64",
            "normalized_content",
            "metadata",
        }
        _exact_fields(raw_asset, allowed_asset, field)
        asset_key = _required_text(raw_asset["asset_key"], f"{field}.asset_key", maximum=200)
        if asset_key in asset_keys:
            raise PortableContentError(f"duplicate module asset_key: {asset_key}")
        asset_keys.add(asset_key)
        _required_text(raw_asset["name"], f"{field}.name", maximum=500)
        _required_text(raw_asset["media_type"], f"{field}.media_type", maximum=100)
        checksum = _required_text(raw_asset["checksum"], f"{field}.checksum", maximum=64)
        if not _SHA256_RE.fullmatch(checksum):
            raise PortableContentError(f"{field}.checksum must be a lowercase SHA-256")
        size = raw_asset["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PortableContentError(f"{field}.size must be a non-negative integer")
        encoded = raw_asset["data_base64"]
        normalized = raw_asset["normalized_content"]
        if not isinstance(encoded, str):
            raise PortableContentError(f"{field}.data_base64 must contain embedded bytes")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise PortableContentError(f"{field}.data_base64 is invalid") from exc
        if len(content) != size:
            raise PortableContentError(f"{field}.size does not match embedded bytes")
        if hashlib.sha256(content).hexdigest() != checksum:
            raise PortableContentError(f"{field}.checksum does not match embedded bytes")
        if normalized is not None and not isinstance(normalized, str):
            raise PortableContentError(f"{field}.normalized_content must be a string or null")
        if not isinstance(raw_asset["metadata"], dict):
            raise PortableContentError(f"{field}.metadata must be an object")

    reviews = payload["content_reviews"]
    if not isinstance(reviews, list):
        raise PortableContentError("module_pack.content_reviews must be an array")
    for index, review in enumerate(reviews):
        field = f"module_pack.content_reviews[{index}]"
        if not isinstance(review, dict):
            raise PortableContentError(f"{field} must be an object")
        scene_key = _required_text(review.get("scene_key"), f"{field}.scene_key", maximum=300)
        if scene_key not in scene_keys:
            raise PortableContentError(f"{field}.scene_key does not exist in scene_atlas")
        for key in ("content_key", "content_kind", "normalized_content"):
            _required_text(review.get(key), f"{field}.{key}", maximum=200_000)
        evidence = review.get("evidence")
        if not isinstance(evidence, dict):
            raise PortableContentError(f"{field}.evidence must be an object")
        asset_key = evidence.get("asset_key")
        chunk_hashes = evidence.get("chunk_hashes")
        if asset_key is not None:
            if asset_key not in asset_keys:
                raise PortableContentError(f"{field}.evidence.asset_key is unknown")
            if chunk_hashes:
                raise PortableContentError(f"{field}.evidence cannot mix asset and chunk evidence")
            page = evidence.get("page")
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise PortableContentError(
                    f"{field}.evidence.page must be a positive integer for asset evidence"
                )
        elif not isinstance(chunk_hashes, list) or not chunk_hashes:
            raise PortableContentError(f"{field}.evidence requires asset_key or chunk_hashes")
        if not isinstance(review.get("metadata", {}), dict):
            raise PortableContentError(f"{field}.metadata must be an object")

    actors = payload["actors"]
    if not isinstance(actors, list):
        raise PortableContentError("module_pack.actors must be an array")
    actor_ids: set[str] = set()
    for index, actor in enumerate(actors):
        card = validate_actor_card(actor, expected_system_id=value["system_id"])
        if card["id"] in actor_ids:
            raise PortableContentError(f"duplicate module actor card id: {card['id']}")
        actor_ids.add(card["id"])
        for binding_index, binding in enumerate(card["payload"]["bindings"]):
            if binding.get("kind") not in {"module", "module_scene"}:
                continue
            if binding.get("module_key") != source["source_key"]:
                raise PortableContentError(
                    f"module actor {card['id']} binding {binding_index} has a different module_key"
                )
            if binding.get("kind") == "module_scene" and binding.get("scene_key") not in scene_keys:
                raise PortableContentError(
                    f"module actor {card['id']} binding {binding_index} has an unknown scene_key"
                )
    return value


def build_preset_pack(
    *,
    portable_id: str,
    version: str,
    system_id: str,
    cards: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any] | None = None,
    dependencies: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a reusable library of actor cards such as an SRD monster pack."""

    envelope = build_portable_envelope(
        kind="preset_pack",
        portable_id=portable_id,
        version=version,
        system_id=system_id,
        payload={
            "preset_schema": PRESET_PACK_SCHEMA,
            "cards": copy.deepcopy(list(cards)),
        },
        metadata=metadata,
        dependencies=dependencies,
    )
    return validate_preset_pack(envelope)


def validate_preset_pack(
    envelope: Mapping[str, Any], *, expected_system_id: str | None = None
) -> dict[str, Any]:
    value = validate_portable_envelope(envelope, expected_kind="preset_pack")
    if expected_system_id is not None and value["system_id"] != expected_system_id:
        raise PortableContentError(
            f"preset pack system_id must be {expected_system_id!r}"
        )
    payload = value["payload"]
    _exact_fields(payload, {"preset_schema", "cards"}, "preset pack payload")
    if payload["preset_schema"] != PRESET_PACK_SCHEMA:
        raise PortableContentError(f"preset_schema must be {PRESET_PACK_SCHEMA!r}")
    if not isinstance(payload["cards"], list) or not payload["cards"]:
        raise PortableContentError("preset_pack.cards must be a non-empty array")
    seen: set[str] = set()
    for card in payload["cards"]:
        normalized = validate_actor_card(card, expected_system_id=value["system_id"])
        if normalized["id"] in seen:
            raise PortableContentError(f"duplicate preset actor card id: {normalized['id']}")
        seen.add(normalized["id"])
    return value


def dumps_portable(envelope: Mapping[str, Any], *, indent: int = 2) -> str:
    """Validate and serialize a portable envelope as UTF-8 friendly JSON."""

    return json.dumps(
        validate_portable_envelope(envelope),
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
    ) + "\n"


def loads_portable(content: str | bytes) -> dict[str, Any]:
    """Parse and validate portable JSON."""

    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PortableContentError("portable content must be UTF-8 JSON") from exc
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PortableContentError(f"invalid portable JSON: {exc.msg}") from exc
    return validate_portable_envelope(value)


def _validate_dependency(value: Any, index: int) -> dict[str, Any]:
    field = f"portable.dependencies[{index}]"
    if not isinstance(value, dict):
        raise PortableContentError(f"{field} must be an object")
    allowed = {"kind", "id", "version", "checksum", "optional"}
    required = {"kind", "id", "version", "optional"}
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown or missing:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if unknown:
            parts.append("unsupported " + ", ".join(unknown))
        raise PortableContentError(f"{field}: " + "; ".join(parts))
    result = copy.deepcopy(value)
    _required_text(result["kind"], f"{field}.kind", maximum=50)
    dependency_id = _required_text(result["id"], f"{field}.id", maximum=200)
    if not _PORTABLE_ID_RE.fullmatch(dependency_id):
        raise PortableContentError(f"{field}.id is invalid")
    _required_text(result["version"], f"{field}.version", maximum=100)
    if not isinstance(result["optional"], bool):
        raise PortableContentError(f"{field}.optional must be a boolean")
    checksum = result.get("checksum")
    if checksum is not None and (
        not isinstance(checksum, str) or not _SHA256_RE.fullmatch(checksum)
    ):
        raise PortableContentError(f"{field}.checksum must be a lowercase SHA-256")
    return result


def _required_text(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortableContentError(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise PortableContentError(f"{field} exceeds {maximum} characters")
    return value


def _exact_fields(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(allowed - set(value))
    if missing:
        raise PortableContentError(f"{field} is missing: " + ", ".join(missing))
    if unknown:
        raise PortableContentError(
            f"{field} has unsupported fields: " + ", ".join(unknown)
        )
