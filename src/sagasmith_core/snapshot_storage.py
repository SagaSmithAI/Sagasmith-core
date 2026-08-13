"""Single-record compressed storage for materialized campaign snapshots."""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from typing import Any

from sagasmith_core.integrity import canonical_json, json_sha256

SNAPSHOT_CODEC = "zlib-1"
MAX_SNAPSHOT_PAYLOAD_BYTES = 64 * 1024 * 1024


class SnapshotStorageError(ValueError):
    """The stored snapshot envelope cannot produce one canonical state document."""


@dataclass(frozen=True)
class EncodedSnapshot:
    compressed_payload: bytes
    payload_codec: str
    uncompressed_size: int
    payload_checksum: str
    record_checksum: str


def _record_metadata(
    *,
    schema_version: int,
    snapshot_id: str,
    campaign_id: str,
    branch_id: str | None,
    parent_id: str | None,
    slot: int,
    payload_codec: str,
    uncompressed_size: int,
    payload_checksum: str,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "snapshot_id": snapshot_id,
        "campaign_id": campaign_id,
        "branch_id": branch_id,
        "parent_id": parent_id,
        "slot": slot,
        "payload_codec": payload_codec,
        "uncompressed_size": uncompressed_size,
        "payload_checksum": payload_checksum,
    }


def snapshot_record_checksum(
    *,
    schema_version: int,
    snapshot_id: str,
    campaign_id: str,
    branch_id: str | None,
    parent_id: str | None,
    slot: int,
    payload_codec: str,
    uncompressed_size: int,
    payload_checksum: str,
    compressed_payload: bytes,
) -> str:
    """Authenticate the stored bytes and their immutable snapshot identity."""

    metadata = _record_metadata(
        schema_version=schema_version,
        snapshot_id=snapshot_id,
        campaign_id=campaign_id,
        branch_id=branch_id,
        parent_id=parent_id,
        slot=slot,
        payload_codec=payload_codec,
        uncompressed_size=uncompressed_size,
        payload_checksum=payload_checksum,
    )
    digest = hashlib.sha256()
    digest.update(canonical_json(metadata).encode("utf-8"))
    digest.update(b"\0")
    digest.update(compressed_payload)
    return digest.hexdigest()


def encode_snapshot_payload(
    payload: dict[str, Any],
    *,
    schema_version: int,
    snapshot_id: str,
    campaign_id: str,
    branch_id: str | None,
    parent_id: str | None,
    slot: int,
) -> EncodedSnapshot:
    raw = canonical_json(payload).encode("utf-8")
    if len(raw) > MAX_SNAPSHOT_PAYLOAD_BYTES:
        raise SnapshotStorageError("snapshot payload exceeds the maximum uncompressed size")
    compressed = zlib.compress(raw)
    payload_checksum = json_sha256(payload)
    record_checksum = snapshot_record_checksum(
        schema_version=schema_version,
        snapshot_id=snapshot_id,
        campaign_id=campaign_id,
        branch_id=branch_id,
        parent_id=parent_id,
        slot=slot,
        payload_codec=SNAPSHOT_CODEC,
        uncompressed_size=len(raw),
        payload_checksum=payload_checksum,
        compressed_payload=compressed,
    )
    return EncodedSnapshot(
        compressed_payload=compressed,
        payload_codec=SNAPSHOT_CODEC,
        uncompressed_size=len(raw),
        payload_checksum=payload_checksum,
        record_checksum=record_checksum,
    )


def decode_snapshot_payload(
    *,
    schema_version: int,
    snapshot_id: str,
    campaign_id: str,
    branch_id: str | None,
    parent_id: str | None,
    slot: int,
    payload_codec: str,
    uncompressed_size: int,
    payload_checksum: str,
    record_checksum: str,
    compressed_payload: bytes,
) -> dict[str, Any]:
    """Decode one self-contained record with bounded allocation and full verification."""

    if payload_codec != SNAPSHOT_CODEC:
        raise SnapshotStorageError("snapshot payload codec is unsupported")
    if not 0 <= uncompressed_size <= MAX_SNAPSHOT_PAYLOAD_BYTES:
        raise SnapshotStorageError("snapshot payload declares an invalid uncompressed size")
    expected_record_checksum = snapshot_record_checksum(
        schema_version=schema_version,
        snapshot_id=snapshot_id,
        campaign_id=campaign_id,
        branch_id=branch_id,
        parent_id=parent_id,
        slot=slot,
        payload_codec=payload_codec,
        uncompressed_size=uncompressed_size,
        payload_checksum=payload_checksum,
        compressed_payload=compressed_payload,
    )
    if expected_record_checksum != record_checksum:
        raise SnapshotStorageError("snapshot record failed checksum verification")

    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(compressed_payload, uncompressed_size + 1)
    except zlib.error as exc:
        raise SnapshotStorageError("snapshot payload decompression failed") from exc
    if (
        len(raw) != uncompressed_size
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise SnapshotStorageError("snapshot payload decompressed size is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotStorageError("snapshot payload JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise SnapshotStorageError("snapshot payload must be a JSON object")
    if canonical_json(payload).encode("utf-8") != raw:
        raise SnapshotStorageError("snapshot payload is not canonically encoded")
    if json_sha256(payload) != payload_checksum:
        raise SnapshotStorageError("snapshot payload failed checksum verification")
    return payload
