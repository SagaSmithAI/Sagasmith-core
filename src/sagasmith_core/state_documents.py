"""Persistence helpers for content-addressed reversible state documents."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from sagasmith_core.models import StateDocument
from sagasmith_core.state_document_storage import (
    StateDocumentStorageError,
    decode_state_document,
    encode_state_document,
)


def persist_state_documents(
    session,
    values: list[dict[str, Any] | None],
) -> list[str | None]:
    """Store each non-null document once and return stable checksum references."""

    encoded_values = [
        encode_state_document(value) if value is not None else None for value in values
    ]
    encoded_by_id = {
        encoded.document_id: encoded for encoded in encoded_values if encoded is not None
    }
    existing = {
        row.id: row
        for row in session.scalars(
            select(StateDocument).where(StateDocument.id.in_(encoded_by_id))
        )
    }
    for document_id, row in existing.items():
        decoded = decode_state_document(
            document_id=row.id,
            payload_codec=row.payload_codec,
            uncompressed_size=row.uncompressed_size,
            compressed_payload=bytes(row.compressed_payload),
        )
        if encode_state_document(decoded).document_id != document_id:
            raise StateDocumentStorageError("stored state document identity is inconsistent")
    for document_id, encoded in encoded_by_id.items():
        if document_id in existing:
            continue
        session.add(
            StateDocument(
                id=document_id,
                payload_codec=encoded.payload_codec,
                uncompressed_size=encoded.uncompressed_size,
                compressed_payload=encoded.compressed_payload,
            )
        )
    return [encoded.document_id if encoded is not None else None for encoded in encoded_values]


def load_state_document(session, document_id: str | None) -> dict[str, Any] | None:
    if document_id is None:
        return None
    row = session.get(StateDocument, document_id)
    if row is None:
        raise StateDocumentStorageError(f"state document is unavailable: {document_id}")
    return decode_state_document(
        document_id=row.id,
        payload_codec=row.payload_codec,
        uncompressed_size=row.uncompressed_size,
        compressed_payload=bytes(row.compressed_payload),
    )
