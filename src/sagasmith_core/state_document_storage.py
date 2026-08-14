"""Canonical compressed storage for immutable reversible state documents."""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from typing import Any

from sagasmith_core.integrity import canonical_json, json_sha256

STATE_DOCUMENT_CODEC = "zlib-1"
MAX_STATE_DOCUMENT_BYTES = 64 * 1024 * 1024


class StateDocumentStorageError(ValueError):
    """A stored state document is malformed, unsupported, or corrupt."""


@dataclass(frozen=True)
class EncodedStateDocument:
    document_id: str
    compressed_payload: bytes
    payload_codec: str
    uncompressed_size: int


def encode_state_document(value: dict[str, Any]) -> EncodedStateDocument:
    if not isinstance(value, dict):
        raise StateDocumentStorageError("state document must be a JSON object")
    raw = canonical_json(value).encode("utf-8")
    if len(raw) > MAX_STATE_DOCUMENT_BYTES:
        raise StateDocumentStorageError("state document exceeds the maximum uncompressed size")
    return EncodedStateDocument(
        document_id=json_sha256(value),
        compressed_payload=zlib.compress(raw),
        payload_codec=STATE_DOCUMENT_CODEC,
        uncompressed_size=len(raw),
    )


def decode_state_document(
    *,
    document_id: str,
    payload_codec: str,
    uncompressed_size: int,
    compressed_payload: bytes,
) -> dict[str, Any]:
    if payload_codec != STATE_DOCUMENT_CODEC:
        raise StateDocumentStorageError("state document codec is unsupported")
    if not 0 <= uncompressed_size <= MAX_STATE_DOCUMENT_BYTES:
        raise StateDocumentStorageError("state document declares an invalid uncompressed size")
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(compressed_payload, uncompressed_size + 1)
    except zlib.error as exc:
        raise StateDocumentStorageError("state document decompression failed") from exc
    if (
        len(raw) != uncompressed_size
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise StateDocumentStorageError("state document decompressed size is invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateDocumentStorageError("state document JSON is invalid") from exc
    if not isinstance(value, dict):
        raise StateDocumentStorageError("state document must be a JSON object")
    if canonical_json(value).encode("utf-8") != raw:
        raise StateDocumentStorageError("state document is not canonically encoded")
    if json_sha256(value) != document_id:
        raise StateDocumentStorageError("state document failed checksum verification")
    return value
