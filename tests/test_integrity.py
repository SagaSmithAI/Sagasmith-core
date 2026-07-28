from sagasmith_core.idempotency import request_hash
from sagasmith_core.integrity import canonical_json, json_sha256
from sagasmith_core.rule_packs import content_checksum


def test_persisted_json_contracts_share_one_canonical_encoding() -> None:
    value = {"z": ["雪", 1], "a": {"enabled": True}}

    assert canonical_json(value) == '{"a":{"enabled":true},"z":["雪",1]}'
    assert request_hash(value) == json_sha256(value)
    assert content_checksum(value) == json_sha256(value)
