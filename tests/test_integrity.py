from sagasmith_core.documents import DOCUMENT_SOURCE_SUFFIXES
from sagasmith_core.idempotency import request_hash
from sagasmith_core.integrity import (
    canonical_json,
    json_sha256,
    unique_retired_source_key,
)
from sagasmith_core.rule_packs import content_checksum


def test_persisted_json_contracts_share_one_canonical_encoding() -> None:
    value = {"z": ["雪", 1], "a": {"enabled": True}}

    assert canonical_json(value) == '{"a":{"enabled":true},"z":["雪",1]}'
    assert request_hash(value) == json_sha256(value)
    assert content_checksum(value) == json_sha256(value)


def test_retired_source_keys_share_one_collision_contract() -> None:
    occupied: set[str] = set()

    first = unique_retired_source_key(
        "x" * 250,
        "abcdef0123456789",
        exists=occupied.__contains__,
    )
    occupied.add(first)
    second = unique_retired_source_key(
        "x" * 250,
        "abcdef0123456789",
        exists=occupied.__contains__,
    )

    assert first == f"{'x' * 180}@abcdef012345"
    assert second == f"{first}-2"
    assert len(second) <= 200


def test_document_importers_share_one_supported_suffix_contract() -> None:
    assert DOCUMENT_SOURCE_SUFFIXES == {".md", ".markdown", ".pdf", ".txt"}
