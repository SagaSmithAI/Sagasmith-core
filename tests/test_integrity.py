from sagasmith_core.documents import DOCUMENT_SOURCE_SUFFIXES
from sagasmith_core.idempotency import request_hash
from sagasmith_core.integrity import (
    canonical_json,
    json_sha256,
    sign_canonical_envelope,
    unique_retired_source_key,
    verify_canonical_envelope,
)


def test_persisted_json_contracts_share_one_canonical_encoding() -> None:
    value = {"z": ["雪", 1], "a": {"enabled": True}}

    assert canonical_json(value) == '{"a":{"enabled":true},"z":["雪",1]}'
    assert request_hash(value) == json_sha256(value)


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


def test_canonical_hmac_envelope_detects_tampering() -> None:
    signed = sign_canonical_envelope({"kind": "context", "revision": 4}, b"secret")
    assert verify_canonical_envelope(
        signed,
        b"secret",
        missing_error="missing",
        invalid_error="invalid",
    ) == {"kind": "context", "revision": 4}
    signed["revision"] = 5
    try:
        verify_canonical_envelope(
            signed,
            b"secret",
            missing_error="missing",
            invalid_error="invalid",
        )
    except ValueError as error:
        assert str(error) == "invalid"
    else:
        raise AssertionError("tampered envelope was accepted")
