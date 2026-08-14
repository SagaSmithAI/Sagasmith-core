"""Content-addressed compressed documents for immutable rule-pack versions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select

from sagasmith_core.models import RulePackPayload, RulePackVersion
from sagasmith_core.state_document_storage import (
    decode_state_document,
    encode_state_document,
)


def persist_rule_pack_payload(session, value: dict[str, Any]) -> RulePackPayload:
    encoded = encode_state_document(value)
    row = session.get(RulePackPayload, encoded.document_id)
    if row is None:
        row = RulePackPayload(
            id=encoded.document_id,
            payload_codec=encoded.payload_codec,
            uncompressed_size=encoded.uncompressed_size,
            compressed_payload=encoded.compressed_payload,
        )
        session.add(row)
    else:
        decode_state_document(
            document_id=row.id,
            payload_codec=row.payload_codec,
            uncompressed_size=row.uncompressed_size,
            compressed_payload=bytes(row.compressed_payload),
        )
    return row


def remove_unreferenced_rule_pack_payload(session, document_id: str | None) -> None:
    if not document_id:
        return
    references = session.scalar(
        select(func.count())
        .select_from(RulePackVersion)
        .where(RulePackVersion.payload_document_id == document_id)
    )
    if not references:
        session.execute(delete(RulePackPayload).where(RulePackPayload.id == document_id))
