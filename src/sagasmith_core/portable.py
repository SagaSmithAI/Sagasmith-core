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
import io
import json
import re
import zipfile
from typing import Any, Mapping, Sequence

from sagasmith_core.integrity import canonical_json, json_sha256
from sagasmith_core.parsing import MAX_RULE_SECTION_TITLE_CHARS

PORTABLE_FORMAT = "sagasmith.portable"
PORTABLE_SCHEMA_VERSION = 1
PORTABLE_KINDS = frozenset(
    {
        "addon_pack",
        "actor_card",
        "module_pack",
        "preset_pack",
        "release_manifest",
        "rule_pack",
    }
)
ADDON_PACK_SCHEMA = "sagasmith.addon-pack.v1"
ADDON_READINESS_SCHEMA_VERSION = 1
ACTOR_CARD_SCHEMA = "sagasmith.actor-card.v2"
ACTOR_CARD_IMAGE_MEDIA_TYPES = frozenset(
    {"image/avif", "image/jpeg", "image/png", "image/webp"}
)
MAX_ACTOR_CARD_IMAGE_BYTES = 8 * 1024 * 1024
MODULE_PACK_SCHEMA = "sagasmith.module-pack.v2"
MODULE_READINESS_SCHEMA_VERSION = 1
MODULE_COMPONENT_NAMES = (
    "source",
    "document",
    "scene_atlas",
    "assets",
    "content_reviews",
    "actors",
    "catalogs",
    "narrative",
)
MODULE_CLASSIFICATIONS = frozenset(
    {
        "campaign",
        "adventure",
        "campaign_chapter",
        "supplement",
        "dm_guide",
        "player_aid",
        "map_pack",
        "pregenerated_character_pack",
    }
)
MODULE_READINESS_LEVELS = ("draft", "indexed", "playable", "complete")
MODULE_READINESS_DIMENSIONS = (
    "source",
    "structure",
    "play_profile",
    "catalog",
    "narrative",
    "runtime",
    "portability",
)
PRESET_PACK_SCHEMA = "sagasmith.preset-pack.v1"
RELEASE_MANIFEST_SCHEMA = "sagasmith.release-manifest.v1"
RULE_PACK_SCHEMA = "sagasmith.rule-pack.v1"

_PORTABLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,199}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PortableContentError(ValueError):
    """Raised when a portable envelope is malformed or has been modified."""


def portable_checksum(value: Mapping[str, Any]) -> str:
    """Return the SHA-256 of an envelope without its top-level checksum."""

    unsigned = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
    return json_sha256(unsigned)


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
    image: Mapping[str, Any] | None = None,
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
        "image": copy.deepcopy(dict(image)) if image is not None else None,
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
        "image",
    }
    schema = payload.get("card_schema")
    _exact_fields(payload, allowed, "actor card payload")
    if schema != ACTOR_CARD_SCHEMA:
        raise PortableContentError(f"card_schema must be {ACTOR_CARD_SCHEMA!r}")
    _validate_actor_card_image(payload["image"])
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


def _validate_actor_card_image(image: Any) -> None:
    """Validate the one self-contained image owned by a portable actor card."""

    if image is None:
        return
    if not isinstance(image, dict):
        raise PortableContentError("actor_card.image must be an object or null")
    fields = {
        "media_type",
        "data_base64",
        "checksum",
        "size",
        "alt",
        "license",
        "attribution",
        "source_ref",
    }
    _exact_fields(image, fields, "actor_card.image")
    media_type = _required_text(
        image["media_type"], "actor_card.image.media_type", maximum=50
    )
    if media_type not in ACTOR_CARD_IMAGE_MEDIA_TYPES:
        raise PortableContentError(
            "actor_card.image.media_type must be image/avif, image/jpeg, image/png, or image/webp"
        )
    encoded = _required_text(
        image["data_base64"], "actor_card.image.data_base64", maximum=12_000_000
    )
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise PortableContentError("actor_card.image.data_base64 is invalid") from exc
    if not raw or len(raw) > MAX_ACTOR_CARD_IMAGE_BYTES:
        raise PortableContentError(
            f"actor_card.image must contain 1 to {MAX_ACTOR_CARD_IMAGE_BYTES} bytes"
        )
    signatures = {
        "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
        "image/webp": len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP",
        "image/avif": len(raw) >= 12 and raw[4:8] == b"ftyp" and b"avif" in raw[8:32],
    }
    if not signatures[media_type]:
        raise PortableContentError(
            "actor_card.image bytes do not match actor_card.image.media_type"
        )
    if image["size"] != len(raw):
        raise PortableContentError("actor_card.image.size does not match decoded bytes")
    checksum = _required_text(
        image["checksum"], "actor_card.image.checksum", maximum=64
    )
    if not _SHA256_RE.fullmatch(checksum) or hashlib.sha256(raw).hexdigest() != checksum:
        raise PortableContentError("actor_card.image.checksum does not match decoded bytes")
    for field, maximum in (
        ("alt", 500),
        ("license", 200),
        ("attribution", 2000),
        ("source_ref", 2000),
    ):
        _required_text(image[field], f"actor_card.image.{field}", maximum=maximum)


def _empty_module_catalogs() -> dict[str, list[dict[str, Any]]]:
    return {
        "items": [],
        "encounters": [],
        "hazards": [],
        "handouts": [],
        "mechanics": [],
    }


def _empty_module_narrative() -> dict[str, list[dict[str, Any]]]:
    return {"dossiers": [], "endings": []}


def _module_content_summary(components: Mapping[str, Any]) -> dict[str, int]:
    catalogs = dict(components["catalogs"])
    narrative = dict(components["narrative"])
    return {
        "scenes": len(components["scene_atlas"]),
        "assets": len(components["assets"]),
        "content_reviews": len(components["content_reviews"]),
        "actors": len(components["actors"]),
        "catalog_entries": sum(len(value) for value in catalogs.values()),
        "dossiers": len(narrative["dossiers"]),
        "endings": len(narrative["endings"]),
    }


def _validate_module_source_refs(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PortableContentError(f"{field} must be an array")
    result = copy.deepcopy(value)
    for index, item in enumerate(result):
        item_field = f"{field}[{index}]"
        if not isinstance(item, dict):
            raise PortableContentError(f"{item_field} must be an object")
        _exact_fields(
            item,
            {"source_key", "page", "chunk_hash", "note"},
            item_field,
        )
        _required_text(item["source_key"], f"{item_field}.source_key", maximum=200)
        page = item["page"]
        if page is not None and (
            isinstance(page, bool) or not isinstance(page, int) or page < 1
        ):
            raise PortableContentError(f"{item_field}.page must be null or positive")
        chunk_hash = item["chunk_hash"]
        if chunk_hash is not None and (
            not isinstance(chunk_hash, str) or not _SHA256_RE.fullmatch(chunk_hash)
        ):
            raise PortableContentError(f"{item_field}.chunk_hash must be null or SHA-256")
        if page is None and chunk_hash is None:
            raise PortableContentError(f"{item_field} requires page or chunk_hash")
        if not isinstance(item["note"], str):
            raise PortableContentError(f"{item_field}.note must be a string")
    return result


def _validate_module_ref_targets(
    refs: Sequence[Mapping[str, Any]],
    *,
    field: str,
    source_key: str,
    chunk_hashes: set[str],
) -> None:
    """Reject citations that cannot be resolved inside the signed module source."""

    for index, ref in enumerate(refs):
        if ref["source_key"] != source_key:
            raise PortableContentError(f"{field}[{index}].source_key is not packaged")
        chunk_hash = ref["chunk_hash"]
        if chunk_hash is not None and chunk_hash not in chunk_hashes:
            raise PortableContentError(f"{field}[{index}].chunk_hash is not packaged")


def _validate_module_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PortableContentError("module_pack.manifest must be an object")
    result = copy.deepcopy(value)
    _exact_fields(
        result,
        {
            "title",
            "classification",
            "compatibility",
            "play_profile",
            "continuity",
            "activation",
            "content_summary",
        },
        "module manifest",
    )
    _required_text(result["title"], "module manifest.title", maximum=300)
    if result["classification"] not in MODULE_CLASSIFICATIONS:
        raise PortableContentError("module manifest.classification is unsupported")
    compatibility = result["compatibility"]
    if not isinstance(compatibility, dict):
        raise PortableContentError("module manifest.compatibility must be an object")
    _exact_fields(
        compatibility,
        {"editions", "required_capabilities"},
        "module manifest.compatibility",
    )
    for field in ("editions", "required_capabilities"):
        items = compatibility[field]
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            raise PortableContentError(
                f"module manifest.compatibility.{field} must be a string array"
            )
    profile = result["play_profile"]
    if not isinstance(profile, dict):
        raise PortableContentError("module manifest.play_profile must be an object")
    _exact_fields(
        profile,
        {
            "party_size",
            "starting_level",
            "expected_end_level",
            "advancement",
            "pregenerated_characters",
        },
        "module manifest.play_profile",
    )
    party_size = profile["party_size"]
    if not isinstance(party_size, dict):
        raise PortableContentError("module play_profile.party_size must be an object")
    _exact_fields(
        party_size,
        {"minimum", "maximum", "source_refs"},
        "module play_profile.party_size",
    )
    for field in ("minimum", "maximum"):
        count = party_size[field]
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or count < 1
        ):
            raise PortableContentError(f"module play_profile.party_size.{field} is invalid")
    if (
        party_size["minimum"] is not None
        and party_size["maximum"] is not None
        and party_size["minimum"] > party_size["maximum"]
    ):
        raise PortableContentError("module play_profile party minimum exceeds maximum")
    _validate_module_source_refs(
        party_size["source_refs"], "module play_profile.party_size.source_refs"
    )
    for field in ("starting_level", "expected_end_level"):
        item = profile[field]
        if not isinstance(item, dict):
            raise PortableContentError(f"module play_profile.{field} must be an object")
        _exact_fields(item, {"value", "source_refs"}, f"module play_profile.{field}")
        level = item["value"]
        if level is not None and (
            isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 20
        ):
            raise PortableContentError(f"module play_profile.{field}.value is invalid")
        _validate_module_source_refs(
            item["source_refs"], f"module play_profile.{field}.source_refs"
        )
    advancement = profile["advancement"]
    if not isinstance(advancement, dict):
        raise PortableContentError("module play_profile.advancement must be an object")
    _exact_fields(
        advancement,
        {"modes", "recommended", "source_refs"},
        "module play_profile.advancement",
    )
    modes = advancement["modes"]
    if not isinstance(modes, list) or any(
        item not in {"xp", "milestone", "story", "unknown"} for item in modes
    ):
        raise PortableContentError("module play_profile.advancement.modes is invalid")
    if advancement["recommended"] is not None and advancement["recommended"] not in modes:
        raise PortableContentError("module advancement recommendation must be one of modes")
    _validate_module_source_refs(
        advancement["source_refs"], "module play_profile.advancement.source_refs"
    )
    pregenerated = profile["pregenerated_characters"]
    if not isinstance(pregenerated, dict):
        raise PortableContentError(
            "module play_profile.pregenerated_characters must be an object"
        )
    _exact_fields(
        pregenerated,
        {"available", "applicability", "source_refs"},
        "module play_profile.pregenerated_characters",
    )
    if not isinstance(pregenerated["available"], bool):
        raise PortableContentError("module pregenerated available must be a boolean")
    if not isinstance(pregenerated["applicability"], str):
        raise PortableContentError("module pregenerated applicability must be a string")
    _validate_module_source_refs(
        pregenerated["source_refs"],
        "module play_profile.pregenerated_characters.source_refs",
    )
    continuity = result["continuity"]
    if not isinstance(continuity, dict):
        raise PortableContentError("module manifest.continuity must be an object")
    _exact_fields(
        continuity,
        {"series_id", "order", "continues_from", "state_policy"},
        "module manifest.continuity",
    )
    for field in ("series_id", "continues_from"):
        if continuity[field] is not None:
            _required_text(continuity[field], f"module continuity.{field}", maximum=200)
    order = continuity["order"]
    if order is not None and (
        isinstance(order, bool) or not isinstance(order, int) or order < 1
    ):
        raise PortableContentError("module continuity.order must be null or positive")
    if not isinstance(continuity["state_policy"], dict):
        raise PortableContentError("module continuity.state_policy must be an object")
    activation = result["activation"]
    if not isinstance(activation, dict):
        raise PortableContentError("module manifest.activation must be an object")
    _exact_fields(
        activation,
        {"mode", "default_active"},
        "module manifest.activation",
    )
    if activation["mode"] != "campaign_attach":
        raise PortableContentError("module activation.mode must be campaign_attach")
    if not isinstance(activation["default_active"], bool):
        raise PortableContentError("module activation.default_active must be a boolean")
    summary = result["content_summary"]
    if not isinstance(summary, dict):
        raise PortableContentError("module manifest.content_summary must be an object")
    expected_summary_fields = {
        "scenes",
        "assets",
        "content_reviews",
        "actors",
        "catalog_entries",
        "dossiers",
        "endings",
    }
    _exact_fields(summary, expected_summary_fields, "module manifest.content_summary")
    if any(
        isinstance(summary[field], bool)
        or not isinstance(summary[field], int)
        or summary[field] < 0
        for field in expected_summary_fields
    ):
        raise PortableContentError("module manifest.content_summary counts are invalid")
    return result


def validate_module_readiness(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the seven-dimensional module publication gate."""

    if not isinstance(value, Mapping):
        raise PortableContentError("module readiness must be an object")
    result = copy.deepcopy(dict(value))
    _exact_fields(
        result,
        {"schema_version", "level", "dimensions", "complete"},
        "module readiness",
    )
    if result["schema_version"] != MODULE_READINESS_SCHEMA_VERSION:
        raise PortableContentError("module readiness.schema_version is unsupported")
    if result["level"] not in MODULE_READINESS_LEVELS:
        raise PortableContentError("module readiness.level is unsupported")
    dimensions = result["dimensions"]
    if not isinstance(dimensions, dict):
        raise PortableContentError("module readiness.dimensions must be an object")
    _exact_fields(dimensions, set(MODULE_READINESS_DIMENSIONS), "module readiness.dimensions")
    all_complete = True
    for name in MODULE_READINESS_DIMENSIONS:
        dimension = dimensions[name]
        field = f"module readiness.dimensions.{name}"
        if not isinstance(dimension, dict):
            raise PortableContentError(f"{field} must be an object")
        _exact_fields(dimension, {"complete", "item_count", "blockers"}, field)
        if not isinstance(dimension["complete"], bool):
            raise PortableContentError(f"{field}.complete must be a boolean")
        if (
            isinstance(dimension["item_count"], bool)
            or not isinstance(dimension["item_count"], int)
            or dimension["item_count"] < 0
        ):
            raise PortableContentError(f"{field}.item_count is invalid")
        blockers = dimension["blockers"]
        if not isinstance(blockers, list):
            raise PortableContentError(f"{field}.blockers must be an array")
        for index, blocker in enumerate(blockers):
            blocker_field = f"{field}.blockers[{index}]"
            if not isinstance(blocker, dict):
                raise PortableContentError(f"{blocker_field} must be an object")
            _exact_fields(
                blocker,
                {"code", "message", "source_refs"},
                blocker_field,
            )
            _required_text(blocker["code"], f"{blocker_field}.code", maximum=100)
            _required_text(blocker["message"], f"{blocker_field}.message", maximum=2000)
            _validate_module_source_refs(
                blocker["source_refs"], f"{blocker_field}.source_refs"
            )
        if dimension["complete"] != (not blockers):
            raise PortableContentError(
                f"{field}.complete must equal whether blockers are empty"
            )
        all_complete = all_complete and dimension["complete"]
    if not isinstance(result["complete"], bool):
        raise PortableContentError("module readiness.complete must be a boolean")
    if result["complete"] != all_complete:
        raise PortableContentError("module readiness.complete must equal all dimensions")
    if result["level"] == "complete" and not result["complete"]:
        raise PortableContentError("complete module readiness cannot have blockers")
    if result["level"] in {"playable", "complete"}:
        for required in ("source", "structure", "play_profile", "runtime", "portability"):
            if not dimensions[required]["complete"]:
                raise PortableContentError(
                    f"{result['level']} module requires complete {required} readiness"
                )
    return result


def build_module_pack(
    *,
    portable_id: str,
    version: str,
    system_id: str,
    manifest: Mapping[str, Any],
    source: Mapping[str, Any],
    document: Mapping[str, Any],
    scene_atlas: Sequence[Mapping[str, Any]],
    assets: Sequence[Mapping[str, Any]] | None = None,
    content_reviews: Sequence[Mapping[str, Any]] | None = None,
    actors: Sequence[Mapping[str, Any]] | None = None,
    catalogs: Mapping[str, Any] | None = None,
    narrative: Mapping[str, Any] | None = None,
    readiness: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
    dependencies: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one immutable v2 module descriptor with locked logical components."""

    component_values = {
        "source": copy.deepcopy(dict(source)),
        "document": copy.deepcopy(dict(document)),
        "scene_atlas": copy.deepcopy(list(scene_atlas)),
        "assets": copy.deepcopy(list(assets or [])),
        "content_reviews": copy.deepcopy(list(content_reviews or [])),
        "actors": copy.deepcopy(list(actors or [])),
        "catalogs": copy.deepcopy(dict(catalogs or _empty_module_catalogs())),
        "narrative": copy.deepcopy(dict(narrative or _empty_module_narrative())),
    }
    normalized_manifest = copy.deepcopy(dict(manifest))
    normalized_manifest["content_summary"] = _module_content_summary(component_values)
    component_locks = [
        {
            "component": name,
            "checksum": hashlib.sha256(
                canonical_json(component_values[name]).encode("utf-8")
            ).hexdigest(),
        }
        for name in MODULE_COMPONENT_NAMES
    ]

    envelope = build_portable_envelope(
        kind="module_pack",
        portable_id=portable_id,
        version=version,
        system_id=system_id,
        payload={
            "module_schema": MODULE_PACK_SCHEMA,
            "manifest": normalized_manifest,
            "component_locks": component_locks,
            **component_values,
            "readiness": copy.deepcopy(dict(readiness)),
        },
        metadata=metadata,
        dependencies=dependencies,
    )
    return validate_module_pack(envelope)


def _validate_module_catalogs(value: Any, scene_keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PortableContentError("module_pack.catalogs must be an object")
    result = copy.deepcopy(value)
    _exact_fields(result, set(_empty_module_catalogs()), "module catalogs")
    seen: set[str] = set()
    for kind, entries in result.items():
        if not isinstance(entries, list):
            raise PortableContentError(f"module catalogs.{kind} must be an array")
        for index, entry in enumerate(entries):
            field = f"module catalogs.{kind}[{index}]"
            if not isinstance(entry, dict):
                raise PortableContentError(f"{field} must be an object")
            _exact_fields(
                entry,
                {"id", "title", "summary", "scene_keys", "source_refs", "definition", "metadata"},
                field,
            )
            item_id = _required_text(entry["id"], f"{field}.id", maximum=200)
            if item_id in seen:
                raise PortableContentError(f"duplicate module catalog id: {item_id}")
            seen.add(item_id)
            _required_text(entry["title"], f"{field}.title", maximum=500)
            if not isinstance(entry["summary"], str):
                raise PortableContentError(f"{field}.summary must be a string")
            bound_scenes = entry["scene_keys"]
            if not isinstance(bound_scenes, list) or any(
                scene_key not in scene_keys for scene_key in bound_scenes
            ):
                raise PortableContentError(f"{field}.scene_keys contains an unknown scene")
            _validate_module_source_refs(entry["source_refs"], f"{field}.source_refs")
            if not isinstance(entry["definition"], dict):
                raise PortableContentError(f"{field}.definition must be an object")
            if not isinstance(entry["metadata"], dict):
                raise PortableContentError(f"{field}.metadata must be an object")
    return result


def _validate_module_narrative(
    value: Any,
    scene_keys: set[str],
    actor_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PortableContentError("module_pack.narrative must be an object")
    result = copy.deepcopy(value)
    _exact_fields(result, {"dossiers", "endings"}, "module narrative")
    seen: set[str] = set()
    for collection, allowed_kinds in (
        ("dossiers", {"actor", "faction", "location", "event", "relationship", "secret"}),
        ("endings", {"ending"}),
    ):
        entries = result[collection]
        if not isinstance(entries, list):
            raise PortableContentError(f"module narrative.{collection} must be an array")
        for index, entry in enumerate(entries):
            field = f"module narrative.{collection}[{index}]"
            if not isinstance(entry, dict):
                raise PortableContentError(f"{field} must be an object")
            _exact_fields(
                entry,
                {
                    "id",
                    "kind",
                    "title",
                    "summary",
                    "scene_keys",
                    "actor_ids",
                    "relationships",
                    "goals",
                    "secrets",
                    "contingencies",
                    "required_context",
                    "source_refs",
                    "metadata",
                },
                field,
            )
            item_id = _required_text(entry["id"], f"{field}.id", maximum=200)
            if item_id in seen:
                raise PortableContentError(f"duplicate module narrative id: {item_id}")
            seen.add(item_id)
            if entry["kind"] not in allowed_kinds:
                raise PortableContentError(f"{field}.kind is unsupported")
            _required_text(entry["title"], f"{field}.title", maximum=500)
            if not isinstance(entry["summary"], str):
                raise PortableContentError(f"{field}.summary must be a string")
            if not isinstance(entry["scene_keys"], list) or any(
                scene_key not in scene_keys for scene_key in entry["scene_keys"]
            ):
                raise PortableContentError(f"{field}.scene_keys contains an unknown scene")
            if not isinstance(entry["actor_ids"], list) or any(
                actor_id not in actor_ids for actor_id in entry["actor_ids"]
            ):
                raise PortableContentError(f"{field}.actor_ids contains an unknown actor")
            for list_field in (
                "relationships",
                "goals",
                "secrets",
                "contingencies",
                "required_context",
            ):
                items = entry[list_field]
                if not isinstance(items, list) or any(
                    not isinstance(item, str) or not item.strip() for item in items
                ):
                    raise PortableContentError(f"{field}.{list_field} is invalid")
            _validate_module_source_refs(entry["source_refs"], f"{field}.source_refs")
            if not isinstance(entry["metadata"], dict):
                raise PortableContentError(f"{field}.metadata must be an object")
    return result


def validate_module_pack(
    envelope: Mapping[str, Any], *, expected_system_id: str | None = None
) -> dict[str, Any]:
    """Validate module content, stable references, embedded files, and actor cards."""

    value = validate_portable_envelope(envelope, expected_kind="module_pack")
    if expected_system_id is not None and value["system_id"] != expected_system_id:
        raise PortableContentError(
            f"module pack system_id must be {expected_system_id!r}"
        )
    for index, dependency in enumerate(value["dependencies"]):
        if dependency["kind"] not in {"rule_pack", "module_pack"}:
            raise PortableContentError(
                f"module dependency {index} must be a rule_pack or module_pack"
            )
        if not dependency.get("checksum"):
            raise PortableContentError(
                f"module dependency {index} requires an exact checksum"
            )
    payload = value["payload"]
    allowed = {
        "module_schema",
        "manifest",
        "component_locks",
        "source",
        "document",
        "scene_atlas",
        "assets",
        "content_reviews",
        "actors",
        "catalogs",
        "narrative",
        "readiness",
    }
    _exact_fields(payload, allowed, "module pack payload")
    if payload["module_schema"] != MODULE_PACK_SCHEMA:
        raise PortableContentError(f"module_schema must be {MODULE_PACK_SCHEMA!r}")
    manifest = _validate_module_manifest(payload["manifest"])
    component_locks = payload["component_locks"]
    if not isinstance(component_locks, list) or len(component_locks) != len(
        MODULE_COMPONENT_NAMES
    ):
        raise PortableContentError("module_pack.component_locks is incomplete")
    lock_values: dict[str, str] = {}
    for index, lock in enumerate(component_locks):
        field = f"module_pack.component_locks[{index}]"
        if not isinstance(lock, dict):
            raise PortableContentError(f"{field} must be an object")
        _exact_fields(lock, {"component", "checksum"}, field)
        component = _required_text(lock["component"], f"{field}.component", maximum=100)
        if component not in MODULE_COMPONENT_NAMES or component in lock_values:
            raise PortableContentError(f"{field}.component is invalid or duplicated")
        checksum = _required_text(lock["checksum"], f"{field}.checksum", maximum=64)
        if not _SHA256_RE.fullmatch(checksum):
            raise PortableContentError(f"{field}.checksum must be a SHA-256")
        lock_values[component] = checksum
    if set(lock_values) != set(MODULE_COMPONENT_NAMES):
        raise PortableContentError("module_pack.component_locks is incomplete")
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
    scene_chunk_hashes: dict[str, set[str]] = {}
    for index, raw_scene in enumerate(scene_atlas):
        field = f"module_pack.scene_atlas[{index}]"
        if not isinstance(raw_scene, dict):
            raise PortableContentError(f"{field} must be an object")
        key = _required_text(raw_scene.get("stable_key"), f"{field}.stable_key", maximum=300)
        if key in scene_keys:
            raise PortableContentError(f"duplicate module scene stable_key: {key}")
        scene_keys.add(key)
        scene_chunk_hashes[key] = set()
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
            scene_chunk_hashes[key].add(chunk_hash)

    all_chunk_hashes = {
        chunk_hash for hashes in scene_chunk_hashes.values() for chunk_hash in hashes
    }

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
            "blob_key",
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
        blob_key = raw_asset["blob_key"]
        normalized = raw_asset["normalized_content"]
        if blob_key != f"blobs/sha256/{checksum}":
            raise PortableContentError(f"{field}.blob_key does not match checksum")
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
        else:
            if not isinstance(chunk_hashes, list) or not chunk_hashes:
                raise PortableContentError(
                    f"{field}.evidence requires asset_key or chunk_hashes"
                )
            if len(set(chunk_hashes)) != len(chunk_hashes) or any(
                not isinstance(chunk_hash, str)
                or not _SHA256_RE.fullmatch(chunk_hash)
                or chunk_hash not in scene_chunk_hashes[scene_key]
                for chunk_hash in chunk_hashes
            ):
                raise PortableContentError(
                    f"{field}.evidence.chunk_hashes must uniquely reference its scene"
                )
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
    catalogs = _validate_module_catalogs(payload["catalogs"], scene_keys)
    narrative = _validate_module_narrative(payload["narrative"], scene_keys, actor_ids)
    readiness = validate_module_readiness(payload["readiness"])
    profile = manifest["play_profile"]
    profile_ref_fields = (
        (profile["party_size"]["source_refs"], "module play_profile.party_size.source_refs"),
        (
            profile["starting_level"]["source_refs"],
            "module play_profile.starting_level.source_refs",
        ),
        (
            profile["expected_end_level"]["source_refs"],
            "module play_profile.expected_end_level.source_refs",
        ),
        (profile["advancement"]["source_refs"], "module play_profile.advancement.source_refs"),
        (
            profile["pregenerated_characters"]["source_refs"],
            "module play_profile.pregenerated_characters.source_refs",
        ),
    )
    for refs, field in profile_ref_fields:
        _validate_module_ref_targets(
            refs,
            field=field,
            source_key=source["source_key"],
            chunk_hashes=all_chunk_hashes,
        )
    for catalog_kind, entries in catalogs.items():
        for index, entry in enumerate(entries):
            _validate_module_ref_targets(
                entry["source_refs"],
                field=f"module catalogs.{catalog_kind}[{index}].source_refs",
                source_key=source["source_key"],
                chunk_hashes=all_chunk_hashes,
            )
    for collection, entries in narrative.items():
        for index, entry in enumerate(entries):
            _validate_module_ref_targets(
                entry["source_refs"],
                field=f"module narrative.{collection}[{index}].source_refs",
                source_key=source["source_key"],
                chunk_hashes=all_chunk_hashes,
            )
    for dimension_name, dimension in readiness["dimensions"].items():
        for index, blocker in enumerate(dimension["blockers"]):
            _validate_module_ref_targets(
                blocker["source_refs"],
                field=(
                    f"module readiness.dimensions.{dimension_name}.blockers[{index}].source_refs"
                ),
                source_key=source["source_key"],
                chunk_hashes=all_chunk_hashes,
            )
    if readiness["dimensions"]["play_profile"]["complete"]:
        if (
            profile["party_size"]["minimum"] is None
            or profile["party_size"]["maximum"] is None
            or profile["starting_level"]["value"] is None
            or profile["expected_end_level"]["value"] is None
            or profile["advancement"]["recommended"] in {None, "unknown"}
            or "unknown" in profile["advancement"]["modes"]
            or any(not refs for refs, _field in profile_ref_fields)
        ):
            raise PortableContentError(
                "complete module play_profile requires sourced party, level, "
                "advancement, and pregenerated-character review"
            )
    if (
        manifest["activation"]["default_active"]
        and readiness["level"] not in {"playable", "complete"}
    ):
        raise PortableContentError(
            "draft or indexed module activation.default_active must be false"
        )
    components = {
        "source": source,
        "document": document,
        "scene_atlas": scene_atlas,
        "assets": assets,
        "content_reviews": reviews,
        "actors": actors,
        "catalogs": catalogs,
        "narrative": narrative,
    }
    for component in MODULE_COMPONENT_NAMES:
        checksum = hashlib.sha256(
            canonical_json(components[component]).encode("utf-8")
        ).hexdigest()
        if lock_values[component] != checksum:
            raise PortableContentError(
                f"module component lock mismatch for {component}"
            )
    if manifest["content_summary"] != _module_content_summary(components):
        raise PortableContentError("module manifest.content_summary does not match components")
    if readiness["level"] == "complete" and not narrative["endings"]:
        raise PortableContentError("complete campaign module requires at least one ending")
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


def portable_rule_chunk_key(
    source_key: str,
    section_ordinal: int,
    chunk_ordinal: int,
    content: str,
) -> str:
    """Return the stable address used for a rule chunk outside one database."""

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"{source_key}/section-{section_ordinal}/chunk-{chunk_ordinal}-{digest}"


def _rule_definition_checksum(envelope: Mapping[str, Any]) -> str:
    """Hash portable rule semantics without distribution-only metadata."""

    definition = {
        "kind": envelope["kind"],
        "id": envelope["id"],
        "version": envelope["version"],
        "system_id": envelope["system_id"],
        "dependencies": copy.deepcopy(envelope["dependencies"]),
        "payload": copy.deepcopy(envelope["payload"]),
    }
    return hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()


def build_rule_pack(
    *,
    portable_id: str,
    version: str,
    system_id: str,
    manifest: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    mechanics: Sequence[Mapping[str, Any]],
    provenance: Mapping[str, Any] | None = None,
    sources: Sequence[Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
    dependencies: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a portable, source-bearing rule-pack definition.

    Runtime source and chunk UUIDs are deliberately not part of this format.
    Callers must replace them with ``source_key`` and ``chunk_key`` before
    building the package.  A receiver creates fresh local identities.
    """

    envelope = build_portable_envelope(
        kind="rule_pack",
        portable_id=portable_id,
        version=version,
        system_id=system_id,
        payload={
            "rule_schema": RULE_PACK_SCHEMA,
            "manifest": copy.deepcopy(dict(manifest)),
            "artifacts": copy.deepcopy([dict(item) for item in artifacts]),
            "mechanics": copy.deepcopy([dict(item) for item in mechanics]),
            "provenance": copy.deepcopy(dict(provenance or {})),
            "sources": copy.deepcopy([dict(item) for item in sources or []]),
        },
        metadata=metadata,
        dependencies=dependencies,
    )
    envelope["metadata"]["definition_checksum"] = _rule_definition_checksum(envelope)
    envelope["checksum"] = portable_checksum(envelope)
    return validate_rule_pack(envelope)


def validate_rule_pack(
    envelope: Mapping[str, Any], *, expected_system_id: str | None = None
) -> dict[str, Any]:
    """Validate the portable boundary around a system-owned rule definition."""

    value = validate_portable_envelope(envelope, expected_kind="rule_pack")
    if expected_system_id is not None and value["system_id"] != expected_system_id:
        raise PortableContentError(f"rule pack system_id must be {expected_system_id!r}")
    payload = value["payload"]
    _exact_fields(
        payload,
        {
            "rule_schema",
            "manifest",
            "artifacts",
            "mechanics",
            "provenance",
            "sources",
        },
        "rule pack payload",
    )
    if payload["rule_schema"] != RULE_PACK_SCHEMA:
        raise PortableContentError(f"rule_schema must be {RULE_PACK_SCHEMA!r}")
    manifest = payload["manifest"]
    if not isinstance(manifest, dict):
        raise PortableContentError("rule_pack.manifest must be an object")
    if manifest.get("id") != value["id"]:
        raise PortableContentError("rule_pack.manifest.id must match portable.id")
    if manifest.get("version") != value["version"]:
        raise PortableContentError("rule_pack.manifest.version must match portable.version")
    if manifest.get("system_id") != value["system_id"]:
        raise PortableContentError("rule_pack.manifest.system_id must match portable.system_id")
    for field in ("artifacts", "mechanics"):
        if not isinstance(payload[field], list) or any(
            not isinstance(item, dict) for item in payload[field]
        ):
            raise PortableContentError(f"rule_pack.{field} must be an object array")
    if not isinstance(payload["provenance"], dict):
        raise PortableContentError("rule_pack.provenance must be an object")
    if not isinstance(payload["sources"], list) or not payload["sources"]:
        raise PortableContentError("rule_pack.sources must be a non-empty array")

    dependency_identities: set[tuple[str, str]] = set()
    dependency_locks: set[tuple[str, str, str]] = set()
    for index, dependency in enumerate(value["dependencies"]):
        if dependency["kind"] != "rule_pack":
            raise PortableContentError(f"rule pack dependency {index} must have kind='rule_pack'")
        if dependency.get("checksum") is None:
            raise PortableContentError(f"rule pack dependency {index} requires an exact checksum")
        if dependency["optional"]:
            raise PortableContentError(f"rule pack dependency {index} cannot be optional")
        identity = (dependency["id"], dependency["version"])
        if identity in dependency_identities:
            raise PortableContentError(
                f"duplicate rule pack dependency: {dependency['id']}@{dependency['version']}"
            )
        if dependency["id"] == value["id"]:
            raise PortableContentError("a rule pack cannot depend on itself")
        dependency_identities.add(identity)
        dependency_locks.add(
            (
                dependency["id"],
                dependency["version"],
                dependency["checksum"],
            )
        )

    manifest_dependencies = manifest.get("dependencies", [])
    if not isinstance(manifest_dependencies, list):
        raise PortableContentError("rule_pack.manifest.dependencies must be an array")
    if any(
        not isinstance(item, dict) or set(item) != {"id", "version", "checksum"}
        for item in manifest_dependencies
    ):
        raise PortableContentError(
            "rule_pack.manifest.dependencies must contain exactly id, version, and checksum"
        )
    manifest_locks = {
        (
            _required_text(item["id"], "rule dependency id", maximum=200),
            _required_text(item["version"], "rule dependency version", maximum=100),
            _required_text(item["checksum"], "rule dependency checksum", maximum=64),
        )
        for item in manifest_dependencies
    }
    if len(manifest_locks) != len(manifest_dependencies):
        raise PortableContentError("rule_pack.manifest.dependencies must be unique")
    if any(not _SHA256_RE.fullmatch(item[2]) for item in manifest_locks):
        raise PortableContentError(
            "rule_pack.manifest dependency checksums must be lowercase SHA-256"
        )
    if manifest_locks != dependency_locks:
        raise PortableContentError(
            "rule_pack manifest and portable dependency locks must match exactly"
        )

    source_keys: set[str] = set()
    chunk_keys: set[str] = set()
    chunk_sources: dict[str, str] = {}
    source_checksums: dict[str, str] = {}
    source_document_checksums: dict[str, str] = {}
    for index, source in enumerate(payload["sources"]):
        normalized = _validate_portable_rule_source(source, index)
        source_key = normalized["source_key"]
        if source_key in source_keys:
            raise PortableContentError(f"duplicate portable rule source_key: {source_key}")
        source_keys.add(source_key)
        source_checksums[source_key] = normalized["checksum"]
        document_checksum = normalized["metadata"].get("source_checksum")
        if document_checksum is not None and (
            not isinstance(document_checksum, str) or not _SHA256_RE.fullmatch(document_checksum)
        ):
            raise PortableContentError(
                f"rule_pack.sources[{index}].metadata.source_checksum must be a lowercase SHA-256"
            )
        source_document_checksums[source_key] = document_checksum or normalized["checksum"]
        for section in normalized["sections"]:
            for chunk in section["chunks"]:
                if chunk["key"] in chunk_keys:
                    raise PortableContentError(f"duplicate portable rule chunk key: {chunk['key']}")
                chunk_keys.add(chunk["key"])
                chunk_sources[chunk["key"]] = source_key

    referenced_chunks: set[str] = set()

    def inspect_references(item: Any, field: str) -> None:
        if isinstance(item, dict):
            forbidden = {name for name in ("source_id", "chunk_id") if name in item}
            if forbidden:
                raise PortableContentError(
                    f"{field} contains runtime locator fields: " + ", ".join(sorted(forbidden))
                )
            chunk_key = item.get("chunk_key")
            if chunk_key is not None:
                normalized_chunk_key = _required_text(chunk_key, f"{field}.chunk_key", maximum=500)
                referenced_chunks.add(normalized_chunk_key)
                supplied_source_key = item.get("source_key")
                if supplied_source_key is not None and chunk_sources.get(
                    normalized_chunk_key
                ) not in {None, supplied_source_key}:
                    raise PortableContentError(f"{field}.source_key does not own its chunk_key")
            source_key = item.get("source_key")
            if source_key is not None and (
                "source_checksum" in item
                or "normalized_checksum" in item
                or chunk_key is not None
                or str(item.get("source") or "").startswith("rule-source:")
            ):
                normalized_source_key = _required_text(
                    source_key, f"{field}.source_key", maximum=200
                )
                if normalized_source_key not in source_keys:
                    raise PortableContentError(
                        f"{field}.source_key references an unknown portable source"
                    )
                source_uri = item.get("source")
                if source_uri is not None and source_uri != (
                    f"rule-source:{normalized_source_key}"
                ):
                    raise PortableContentError(f"{field}.source does not match source_key")
                normalized_checksum = item.get("normalized_checksum")
                if (
                    normalized_checksum is not None
                    and normalized_checksum != source_checksums[normalized_source_key]
                ):
                    raise PortableContentError(
                        f"{field}.normalized_checksum does not match source_key"
                    )
                document_checksum = item.get("source_checksum")
                if (
                    document_checksum is not None
                    and document_checksum != source_document_checksums[normalized_source_key]
                ):
                    raise PortableContentError(f"{field}.source_checksum does not match source_key")
            for key, child in item.items():
                inspect_references(child, f"{field}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                inspect_references(child, f"{field}[{index}]")

    inspect_references(payload, "rule_pack")
    missing_chunks = sorted(referenced_chunks - chunk_keys)
    if missing_chunks:
        raise PortableContentError(
            "rule pack references unknown portable chunks: " + ", ".join(missing_chunks)
        )
    definition_checksum = _required_text(
        value["metadata"].get("definition_checksum"),
        "rule_pack.metadata.definition_checksum",
        maximum=64,
    )
    if not _SHA256_RE.fullmatch(definition_checksum):
        raise PortableContentError(
            "rule_pack.metadata.definition_checksum must be a lowercase SHA-256"
        )
    expected_definition_checksum = _rule_definition_checksum(value)
    if definition_checksum != expected_definition_checksum:
        raise PortableContentError(
            "rule pack definition checksum mismatch: expected "
            f"{expected_definition_checksum}, received {definition_checksum}"
        )
    return value


def portable_rule_definition_checksum(envelope: Mapping[str, Any]) -> str:
    """Return the validated, machine-independent rule definition checksum."""

    value = validate_rule_pack(envelope)
    return _rule_definition_checksum(value)


def build_release_manifest(
    *,
    portable_id: str,
    version: str,
    system_id: str,
    components: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a thin manifest that composes independently governed packages."""

    envelope = build_portable_envelope(
        kind="release_manifest",
        portable_id=portable_id,
        version=version,
        system_id=system_id,
        payload={"release_schema": RELEASE_MANIFEST_SCHEMA},
        metadata=metadata,
        dependencies=components,
    )
    return validate_release_manifest(envelope)


def validate_release_manifest(
    envelope: Mapping[str, Any], *, expected_system_id: str | None = None
) -> dict[str, Any]:
    """Validate a release manifest without granting any component authority."""

    value = validate_portable_envelope(envelope, expected_kind="release_manifest")
    if expected_system_id is not None and value["system_id"] != expected_system_id:
        raise PortableContentError(f"release manifest system_id must be {expected_system_id!r}")
    payload = value["payload"]
    _exact_fields(payload, {"release_schema"}, "release manifest payload")
    if payload["release_schema"] != RELEASE_MANIFEST_SCHEMA:
        raise PortableContentError(f"release_schema must be {RELEASE_MANIFEST_SCHEMA!r}")
    if not value["dependencies"]:
        raise PortableContentError("release manifest must reference at least one component")
    supported = {"module_pack", "preset_pack", "rule_pack"}
    identities: set[tuple[str, str, str]] = set()
    has_rule_pack = False
    for index, component in enumerate(value["dependencies"]):
        kind = component["kind"]
        if kind not in supported:
            raise PortableContentError(f"release component {index} has unsupported kind: {kind}")
        if component.get("checksum") is None:
            raise PortableContentError(f"release component {index} requires an exact checksum")
        identity = (kind, component["id"], component["version"])
        if identity in identities:
            raise PortableContentError(
                f"duplicate release component: {kind}:{component['id']}@{component['version']}"
            )
        identities.add(identity)
        has_rule_pack = has_rule_pack or kind == "rule_pack"
    if not has_rule_pack:
        raise PortableContentError("an extension release must include at least one rule_pack")
    return value


def build_addon_pack(
    *,
    portable_id: str,
    version: str,
    system_id: str,
    manifest: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one self-contained, importable addon from portable components.

    ``release_manifest`` intentionally remains a thin lock file.  An addon is
    the distribution unit: it embeds each independently validated component so
    a receiver can inspect and import the whole release without locating
    sidecar files.  Runtime authority still belongs to the component services;
    importing this envelope cannot install or activate any component by itself.
    """

    normalized_components = [
        _validate_addon_component(component, index)
        for index, component in enumerate(components)
    ]
    normalized_components.sort(
        key=lambda item: (item["kind"], item["id"], item["version"])
    )
    locks = [
        {
            "kind": component["kind"],
            "id": component["id"],
            "version": component["version"],
            "checksum": component["checksum"],
            "optional": False,
        }
        for component in normalized_components
    ]
    package_metadata = copy.deepcopy(dict(metadata or {}))
    package_metadata.setdefault("distribution", "private")
    envelope = build_portable_envelope(
        kind="addon_pack",
        portable_id=portable_id,
        version=version,
        system_id=system_id,
        payload={
            "addon_schema": ADDON_PACK_SCHEMA,
            "manifest": copy.deepcopy(dict(manifest)),
            "components": normalized_components,
        },
        metadata=package_metadata,
        dependencies=locks,
    )
    return validate_addon_pack(envelope)


def validate_addon_readiness(readiness: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the system-neutral addon readiness summary.

    Readiness is evidence, not authority.  System plugins must recompute the
    report from embedded content before they rely on it for import or
    activation.  Core only guarantees that a published report is complete,
    internally consistent, and portable across runtimes.
    """

    if not isinstance(readiness, Mapping):
        raise PortableContentError("addon readiness must be an object")
    value = copy.deepcopy(dict(readiness))
    _exact_fields(
        value,
        {"schema_version", "source", "catalog", "selection", "runtime", "complete"},
        "addon readiness",
    )
    if value["schema_version"] != ADDON_READINESS_SCHEMA_VERSION:
        raise PortableContentError(
            "addon readiness.schema_version must be "
            f"{ADDON_READINESS_SCHEMA_VERSION}"
        )

    dimensions = {
        "source": ("item_count", "verified_count"),
        "catalog": ("item_count", "reviewed_count"),
        "selection": ("applicable_count", "ready_count", "not_applicable_count"),
        "runtime": ("item_count", "resolved_count", "modes"),
    }
    normalized: dict[str, dict[str, Any]] = {}
    for dimension, count_fields in dimensions.items():
        raw = value[dimension]
        if not isinstance(raw, Mapping):
            raise PortableContentError(f"addon readiness.{dimension} must be an object")
        item = copy.deepcopy(dict(raw))
        _exact_fields(
            item,
            {"complete", "blockers", *count_fields},
            f"addon readiness.{dimension}",
        )
        if not isinstance(item["complete"], bool):
            raise PortableContentError(
                f"addon readiness.{dimension}.complete must be a boolean"
            )
        for count_field in count_fields:
            if count_field == "modes":
                continue
            count = item[count_field]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise PortableContentError(
                    f"addon readiness.{dimension}.{count_field} "
                    "must be a non-negative integer"
                )
        blockers = item["blockers"]
        if not isinstance(blockers, list):
            raise PortableContentError(
                f"addon readiness.{dimension}.blockers must be an array"
            )
        for index, blocker in enumerate(blockers):
            field = f"addon readiness.{dimension}.blockers[{index}]"
            if not isinstance(blocker, Mapping):
                raise PortableContentError(f"{field} must be an object")
            _exact_fields(
                blocker,
                {"component_id", "item_id", "reason"},
                field,
            )
            _required_text(blocker["component_id"], f"{field}.component_id", maximum=200)
            item_id = blocker["item_id"]
            if item_id is not None:
                _required_text(item_id, f"{field}.item_id", maximum=500)
            _required_text(blocker["reason"], f"{field}.reason", maximum=1000)

        if dimension == "source":
            if item["verified_count"] > item["item_count"]:
                raise PortableContentError(
                    "addon readiness.source.verified_count cannot exceed item_count"
                )
            dimension_complete = item["verified_count"] == item["item_count"]
        elif dimension == "catalog":
            if item["reviewed_count"] > item["item_count"]:
                raise PortableContentError(
                    "addon readiness.catalog.reviewed_count cannot exceed item_count"
                )
            dimension_complete = item["reviewed_count"] == item["item_count"]
        elif dimension == "selection":
            if item["ready_count"] > item["applicable_count"]:
                raise PortableContentError(
                    "addon readiness.selection.ready_count cannot exceed applicable_count"
                )
            dimension_complete = item["ready_count"] == item["applicable_count"]
        else:
            modes = item["modes"]
            if not isinstance(modes, dict) or any(
                not isinstance(mode, str)
                or not mode.strip()
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                for mode, count in modes.items()
            ):
                raise PortableContentError(
                    "addon readiness.runtime.modes must map non-empty mode names "
                    "to non-negative integers"
                )
            if sum(modes.values()) != item["resolved_count"]:
                raise PortableContentError(
                    "addon readiness.runtime.modes must sum to resolved_count"
                )
            if item["resolved_count"] > item["item_count"]:
                raise PortableContentError(
                    "addon readiness.runtime.resolved_count cannot exceed item_count"
                )
            dimension_complete = item["resolved_count"] == item["item_count"]

        dimension_complete = dimension_complete and not blockers
        if item["complete"] != dimension_complete:
            raise PortableContentError(
                f"addon readiness.{dimension}.complete does not match its counts "
                "and blockers"
            )
        normalized[dimension] = item

    if not isinstance(value["complete"], bool):
        raise PortableContentError("addon readiness.complete must be a boolean")
    expected_complete = all(item["complete"] for item in normalized.values())
    if value["complete"] != expected_complete:
        raise PortableContentError(
            "addon readiness.complete must equal all dimension completion states"
        )
    value.update(normalized)
    return value


def validate_addon_pack(
    envelope: Mapping[str, Any], *, expected_system_id: str | None = None
) -> dict[str, Any]:
    """Validate an all-in-one addon without granting runtime authority."""

    value = validate_portable_envelope(envelope, expected_kind="addon_pack")
    if expected_system_id is not None and value["system_id"] != expected_system_id:
        raise PortableContentError(f"addon pack system_id must be {expected_system_id!r}")
    payload = value["payload"]
    _exact_fields(
        payload,
        {"addon_schema", "manifest", "components"},
        "addon pack payload",
    )
    if payload["addon_schema"] != ADDON_PACK_SCHEMA:
        raise PortableContentError(f"addon_schema must be {ADDON_PACK_SCHEMA!r}")
    manifest = payload["manifest"]
    if not isinstance(manifest, dict):
        raise PortableContentError("addon_pack.manifest must be an object")
    for field, expected in (
        ("id", value["id"]),
        ("version", value["version"]),
        ("system_id", value["system_id"]),
    ):
        if manifest.get(field) != expected:
            raise PortableContentError(
                f"addon_pack.manifest.{field} must match portable.{field}"
            )
    _required_text(manifest.get("title"), "addon_pack.manifest.title", maximum=300)
    editions = manifest.get("editions")
    if not isinstance(editions, list) or not editions or any(
        not isinstance(item, str) or not item.strip() for item in editions
    ):
        raise PortableContentError(
            "addon_pack.manifest.editions must be a non-empty string array"
        )
    if len(editions) != len(set(editions)):
        raise PortableContentError("addon_pack.manifest.editions must be unique")
    classification = _required_text(
        manifest.get("classification"),
        "addon_pack.manifest.classification",
        maximum=50,
    )
    if classification not in {
        "official_core",
        "official_supplement",
        "official_legacy",
        "playtest",
        "third_party",
        "homebrew",
    }:
        raise PortableContentError("addon_pack.manifest.classification is unsupported")
    content_summary = manifest.get("content_summary")
    if not isinstance(content_summary, dict) or any(
        not isinstance(kind, str)
        or not kind.strip()
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        for kind, count in content_summary.items()
    ):
        raise PortableContentError(
            "addon_pack.manifest.content_summary must map content kinds to non-negative counts"
        )
    if "readiness" not in manifest:
        raise PortableContentError("addon_pack.manifest.readiness is required")
    manifest["readiness"] = validate_addon_readiness(manifest["readiness"])
    activation = manifest.get("activation")
    if not isinstance(activation, dict):
        raise PortableContentError("addon_pack.manifest.activation must be an object")
    unknown_activation = sorted(
        set(activation) - {"rule_policy", "preset_policy", "module_policy"}
    )
    if unknown_activation:
        raise PortableContentError(
            "addon_pack.manifest.activation has unsupported fields: "
            + ", ".join(unknown_activation)
        )
    if activation.get("rule_policy") not in {"branch", "none"}:
        raise PortableContentError(
            "addon_pack.manifest.activation.rule_policy must be branch or none"
        )
    if activation.get("preset_policy") not in {"library", "none"}:
        raise PortableContentError(
            "addon_pack.manifest.activation.preset_policy must be library or none"
        )
    if activation.get("module_policy") != "none":
        raise PortableContentError(
            "addon_pack.manifest.activation.module_policy must be none; "
            "module releases are distributed separately"
        )
    conflicts = manifest.get("conflicts", [])
    if not isinstance(conflicts, list):
        raise PortableContentError("addon_pack.manifest.conflicts must be an array")
    conflict_ids = []
    for index, conflict in enumerate(conflicts):
        conflict_id = str(
            conflict.get("id") if isinstance(conflict, dict) else conflict
        ).strip()
        if not conflict_id:
            raise PortableContentError(
                f"addon_pack.manifest.conflicts[{index}] must identify an addon"
            )
        if conflict_id == value["id"]:
            raise PortableContentError("an addon cannot conflict with itself")
        conflict_ids.append(conflict_id)
    if len(conflict_ids) != len(set(conflict_ids)):
        raise PortableContentError("addon_pack.manifest.conflicts must be unique")

    raw_components = payload["components"]
    if not isinstance(raw_components, list) or not raw_components:
        raise PortableContentError("addon_pack.components must be a non-empty array")
    components = [
        _validate_addon_component(component, index)
        for index, component in enumerate(raw_components)
    ]
    identities = [(item["kind"], item["id"], item["version"]) for item in components]
    if len(identities) != len(set(identities)):
        raise PortableContentError("addon_pack components must have unique identities")
    if any(item["system_id"] != value["system_id"] for item in components):
        raise PortableContentError("addon_pack components must use the addon system_id")
    component_kinds = {item["kind"] for item in components}
    expected_policy = {
        "rule_pack": ("rule_policy", "branch"),
        "preset_pack": ("preset_policy", "library"),
    }
    for component_kind, (policy_name, active_value) in expected_policy.items():
        actual = activation[policy_name]
        if component_kind in component_kinds and actual != active_value:
            raise PortableContentError(
                f"addon_pack.manifest.activation.{policy_name} must be "
                f"{active_value} when {component_kind} components are embedded"
            )
        if component_kind not in component_kinds and actual != "none":
            raise PortableContentError(
                f"addon_pack.manifest.activation.{policy_name} must be none "
                f"without {component_kind} components"
            )
    addon_editions = {str(item) for item in editions}
    for component in components:
        if component["kind"] != "rule_pack":
            continue
        component_editions = {
            str(item)
            for item in component["payload"]["manifest"].get("editions", [])
        }
        unsupported = sorted(addon_editions - component_editions)
        if unsupported:
            raise PortableContentError(
                f"addon rule component {component['id']} does not support addon "
                "editions: " + ", ".join(unsupported)
            )
    locks = {
        (item["kind"], item["id"], item["version"], item["checksum"])
        for item in components
    }
    dependency_locks = {
        (item["kind"], item["id"], item["version"], item.get("checksum"))
        for item in value["dependencies"]
    }
    if any(item.get("optional") for item in value["dependencies"]):
        raise PortableContentError("addon_pack component locks cannot be optional")
    if locks != dependency_locks or len(locks) != len(value["dependencies"]):
        raise PortableContentError(
            "addon_pack dependencies must exactly lock every embedded component"
        )

    distribution = str(value["metadata"].get("distribution") or "")
    if distribution not in {"private", "shareable"}:
        raise PortableContentError(
            "addon_pack.metadata.distribution must be private or shareable"
        )
    if distribution == "shareable" and (
        not str(value["metadata"].get("license") or "").strip()
        or not str(value["metadata"].get("attribution") or "").strip()
    ):
        raise PortableContentError(
            "shareable addon packs require explicit license and attribution"
        )
    return value


def _validate_addon_component(value: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PortableContentError(f"addon_pack.components[{index}] must be an object")
    kind = str(value.get("kind") or "")
    validators = {
        "preset_pack": validate_preset_pack,
        "rule_pack": validate_rule_pack,
    }
    validator = validators.get(kind)
    if validator is None:
        raise PortableContentError(
            f"addon_pack.components[{index}] has unsupported kind: {kind}"
        )
    return validator(value)


def _validate_portable_rule_source(value: Any, index: int) -> dict[str, Any]:
    field = f"rule_pack.sources[{index}]"
    if not isinstance(value, dict):
        raise PortableContentError(f"{field} must be an object")
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
            raise PortableContentError(
                f"{field}.{name} must be a string up to {maximum} characters"
            )
    canonical = value["canonical_source_key"]
    if canonical is not None:
        _required_text(canonical, f"{field}.canonical_source_key", maximum=200)
    checksum = _required_text(value["checksum"], f"{field}.checksum", maximum=64)
    if not _SHA256_RE.fullmatch(checksum):
        raise PortableContentError(f"{field}.checksum must be a lowercase SHA-256")
    metadata = value["metadata"]
    if not isinstance(metadata, dict):
        raise PortableContentError(f"{field}.metadata must be an object")
    _reject_rule_source_runtime_metadata(metadata, f"{field}.metadata")
    sections = value["sections"]
    if not isinstance(sections, list) or not sections:
        raise PortableContentError(f"{field}.sections must be a non-empty array")
    section_ordinals: set[int] = set()
    for section_index, section in enumerate(sections):
        section_field = f"{field}.sections[{section_index}]"
        if not isinstance(section, dict):
            raise PortableContentError(f"{section_field} must be an object")
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
            raise PortableContentError(
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
            raise PortableContentError(
                f"{section_field}.parent_ordinal must reference an earlier section"
            )
        level = section["level"]
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 6:
            raise PortableContentError(f"{section_field}.level must be from 1 to 6")
        _required_text(
            section["title"],
            f"{section_field}.title",
            maximum=MAX_RULE_SECTION_TITLE_CHARS,
        )
        if not isinstance(section["path"], list) or not section["path"] or any(
            not isinstance(item, str) or not item.strip() for item in section["path"]
        ):
            raise PortableContentError(f"{section_field}.path must be a non-empty string array")
        content = section["content"]
        if not isinstance(content, str):
            raise PortableContentError(f"{section_field}.content must be a string")
        if section["content_hash"] != hashlib.sha256(content.encode("utf-8")).hexdigest():
            raise PortableContentError(f"{section_field}.content_hash mismatch")
        _validate_offsets(section, section_field)
        if section["end_offset"] - section["start_offset"] != len(content):
            raise PortableContentError(f"{section_field} offsets do not match its content length")
        chunks = section["chunks"]
        if not isinstance(chunks, list) or not chunks:
            raise PortableContentError(f"{section_field}.chunks must be a non-empty array")
        chunk_ordinals: set[int] = set()
        for chunk_index, chunk in enumerate(chunks):
            chunk_field = f"{section_field}.chunks[{chunk_index}]"
            if not isinstance(chunk, dict):
                raise PortableContentError(f"{chunk_field} must be an object")
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
                raise PortableContentError(
                    f"{chunk_field}.ordinal must be unique in its section and non-negative"
                )
            chunk_ordinals.add(chunk_ordinal)
            chunk_content = chunk["content"]
            if not isinstance(chunk_content, str):
                raise PortableContentError(f"{chunk_field}.content must be a string")
            chunk_hash = hashlib.sha256(chunk_content.encode("utf-8")).hexdigest()
            if chunk["content_hash"] != chunk_hash:
                raise PortableContentError(f"{chunk_field}.content_hash mismatch")
            expected_key = portable_rule_chunk_key(
                source_key, ordinal, chunk_ordinal, chunk_content
            )
            if chunk["key"] != expected_key:
                raise PortableContentError(f"{chunk_field}.key must be {expected_key!r}")
            if not isinstance(chunk["heading_path"], list) or any(
                not isinstance(item, str) or not item.strip() for item in chunk["heading_path"]
            ):
                raise PortableContentError(f"{chunk_field}.heading_path must be a string array")
            token_count = chunk["token_count"]
            if isinstance(token_count, bool) or not isinstance(token_count, int) or token_count < 0:
                raise PortableContentError(f"{chunk_field}.token_count must be non-negative")
            if not isinstance(chunk["metadata"], dict):
                raise PortableContentError(f"{chunk_field}.metadata must be an object")
            _reject_rule_source_runtime_metadata(chunk["metadata"], f"{chunk_field}.metadata")
    missing_parents = sorted(
        int(section["parent_ordinal"])
        for section in sections
        if section["parent_ordinal"] is not None
        and int(section["parent_ordinal"]) not in section_ordinals
    )
    if missing_parents:
        raise PortableContentError(f"{field} references missing parent sections: {missing_parents}")
    return copy.deepcopy(value)


def validate_portable_rule_source(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one detached rule source used inside a portable rule pack."""

    return _validate_portable_rule_source(value, 0)


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
            raise PortableContentError(
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
        raise PortableContentError(
            f"{field}.start_offset/end_offset must be ordered non-negative integers"
        )


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


def dumps_module_archive(
    package: Mapping[str, Any],
    blobs: Mapping[str, bytes],
) -> bytes:
    """Serialize one v2 module descriptor and its exact content-addressed blobs."""

    value = validate_module_pack(package)
    expected = {
        asset["checksum"]: asset for asset in value["payload"]["assets"]
    }
    normalized_blobs = {
        str(key).removeprefix("blobs/sha256/"): bytes(data)
        for key, data in blobs.items()
    }
    if set(normalized_blobs) != set(expected):
        raise PortableContentError("module archive blobs do not match asset descriptors")
    for checksum, content in normalized_blobs.items():
        asset = expected[checksum]
        if len(content) != asset["size"] or hashlib.sha256(content).hexdigest() != checksum:
            raise PortableContentError(f"module archive blob mismatch: {checksum}")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
        archive.writestr(
            "module.sagasmith.json",
            canonical_json(value).encode("utf-8"),
            compress_type=zipfile.ZIP_DEFLATED,
        )
        for checksum, content in sorted(normalized_blobs.items()):
            archive.writestr(
                f"blobs/sha256/{checksum}",
                content,
                compress_type=zipfile.ZIP_STORED,
            )
    return output.getvalue()


def loads_module_archive(
    content: bytes,
    *,
    maximum_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Load and fully verify a v2 module archive without accepting extra files."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(content), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise PortableContentError("invalid module archive") from exc
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise PortableContentError("module archive contains duplicate paths")
        if "module.sagasmith.json" not in names:
            raise PortableContentError("module archive has no descriptor")
        if any(info.flag_bits & 0x1 for info in infos):
            raise PortableContentError("encrypted module archive entries are not supported")
        if sum(info.file_size for info in infos) > maximum_uncompressed_bytes:
            raise PortableContentError("module archive exceeds the uncompressed safety limit")
        allowed_path = re.compile(r"blobs/sha256/[0-9a-f]{64}")
        unexpected = [
            name
            for name in names
            if name != "module.sagasmith.json" and not allowed_path.fullmatch(name)
        ]
        if unexpected:
            raise PortableContentError("module archive contains unsupported paths")
        try:
            package = json.loads(archive.read("module.sagasmith.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PortableContentError("module archive descriptor is invalid") from exc
        value = validate_module_pack(package)
        blobs = {
            name.removeprefix("blobs/sha256/"): archive.read(name)
            for name in names
            if name.startswith("blobs/sha256/")
        }
    expected = {asset["checksum"] for asset in value["payload"]["assets"]}
    if set(blobs) != expected:
        raise PortableContentError("module archive blobs do not match descriptor")
    for checksum, data in blobs.items():
        asset = next(
            item for item in value["payload"]["assets"] if item["checksum"] == checksum
        )
        if len(data) != asset["size"] or hashlib.sha256(data).hexdigest() != checksum:
            raise PortableContentError(f"module archive blob mismatch: {checksum}")
    return value, blobs


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
