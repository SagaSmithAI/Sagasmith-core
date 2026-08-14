"""Bounded compressed storage for the mutable document of an import job."""

from __future__ import annotations

from typing import Any

from sagasmith_core.state_document_storage import (
    decode_state_document,
    encode_state_document,
)

IMPORT_JOB_DOCUMENT_FIELDS = (
    "payload",
    "inspection",
    "candidates",
    "validation",
    "result",
)


def empty_import_job_document() -> dict[str, Any]:
    return {
        "payload": {},
        "inspection": {},
        "candidates": [],
        "validation": {},
        "result": {},
    }


def encode_import_job_document(value: dict[str, Any]):
    document = empty_import_job_document()
    document.update(value)
    _validate_document(document)
    return encode_state_document(document)


def decode_import_job_document(row: Any) -> dict[str, Any]:
    cached = row.__dict__.get("_decoded_import_job_document")
    if cached is None:
        cached = decode_state_document(
            document_id=row.document_checksum,
            payload_codec=row.document_codec,
            uncompressed_size=row.document_uncompressed_size,
            compressed_payload=bytes(row.compressed_document),
        )
        _validate_document(cached)
        row.__dict__["_decoded_import_job_document"] = cached
    return cached


def apply_import_job_document(row: Any, value: dict[str, Any]) -> None:
    document = empty_import_job_document()
    document.update(value)
    encoded = encode_import_job_document(document)
    row.document_codec = encoded.payload_codec
    row.document_uncompressed_size = encoded.uncompressed_size
    row.document_checksum = encoded.document_id
    row.compressed_document = encoded.compressed_payload
    row.__dict__["_decoded_import_job_document"] = document


def encoded_import_job_columns(value: dict[str, Any]) -> dict[str, Any]:
    encoded = encode_import_job_document(value)
    return {
        "document_codec": encoded.payload_codec,
        "document_uncompressed_size": encoded.uncompressed_size,
        "document_checksum": encoded.document_id,
        "compressed_document": encoded.compressed_payload,
    }


def _validate_document(value: dict[str, Any]) -> None:
    if set(value) != set(IMPORT_JOB_DOCUMENT_FIELDS):
        raise ValueError("import job document has an unsupported shape")
    for field in ("payload", "inspection", "validation", "result"):
        if not isinstance(value[field], dict):
            raise ValueError(f"import job {field} must be an object")
    if not isinstance(value["candidates"], list) or any(
        not isinstance(item, dict) for item in value["candidates"]
    ):
        raise ValueError("import job candidates must be an array of objects")
