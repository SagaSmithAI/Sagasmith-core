import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter

import numpy as np
import pytest

from sagasmith_core.embeddings import (
    BgeEmbedder,
    BgeM3Embedder,
    BgeSmallEnEmbedder,
    BgeSmallZhEmbedder,
    EmbeddingProfile,
    collection_name,
    configured_profiles,
    profile_for_language,
)

TEST_REVISION_1 = "1" * 40
TEST_REVISION_2 = "2" * 40


@pytest.fixture(autouse=True)
def _clear_embedding_process_caches():
    BgeEmbedder._cache.clear()
    BgeEmbedder._models.clear()
    yield
    BgeEmbedder._cache.clear()
    BgeEmbedder._models.clear()


def test_profiles_preserve_legacy_chinese_and_english_choices(monkeypatch) -> None:
    monkeypatch.setenv(
        "DND_EMBEDDING_PROFILES",
        "bge_small_zh_v1_5,bge_small_en_v1_5",
    )

    assert profile_for_language("zh-CN", env_prefix="DND").language == "zh"
    assert profile_for_language("en", env_prefix="DND").language == "en"


def test_bge_m3_is_the_default(monkeypatch) -> None:
    monkeypatch.delenv("GENERIC_EMBEDDING_PROFILES", raising=False)

    assert configured_profiles("GENERIC")[0].model_name == "BAAI/bge-m3"


def test_explicit_embedder_classes_select_expected_profiles(monkeypatch) -> None:
    monkeypatch.setenv("TTRPG_EMBEDDING_MODE", "cpu")

    assert BgeM3Embedder().dimensions == 1024
    assert BgeSmallZhEmbedder().dimensions == 512
    assert BgeSmallEnEmbedder().dimensions == 384


def test_unknown_profile_fails_early(monkeypatch) -> None:
    monkeypatch.setenv("DND_EMBEDDING_PROFILES", "missing")

    with pytest.raises(ValueError):
        configured_profiles("DND")


def test_persistent_cache_survives_restart_and_deduplicates_batches(
    monkeypatch,
    tmp_path,
) -> None:
    profile = EmbeddingProfile("test", "test/model", 2, "multi", TEST_REVISION_1)

    class FakeModel:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def encode(self, texts, **_kwargs):
            self.calls.append(list(texts))
            return np.asarray(
                [[float(len(text)), float(index + 1)] for index, text in enumerate(texts)],
                dtype=np.float32,
            )

    model = FakeModel()
    monkeypatch.setattr(BgeEmbedder, "_load", lambda _self: model)
    first = BgeEmbedder(
        env_prefix="TEST",
        profile=profile,
        device="cpu",
        cache_dir=tmp_path,
    )

    expected = first.encode(["alpha", "alpha", "beta"])

    assert model.calls == [["alpha", "beta"]]
    assert expected == [[5.0, 1.0], [5.0, 1.0], [4.0, 2.0]]
    assert first.persistent_cache_hits == 0
    assert first.persistent_cache_misses == 2
    assert first.persistent_cache is not None
    assert b"alpha" not in first.persistent_cache.path.read_bytes()

    BgeEmbedder._cache.clear()
    monkeypatch.setattr(
        BgeEmbedder,
        "_load",
        lambda _self: pytest.fail("persistent cache should avoid model loading"),
    )
    restarted = BgeEmbedder(
        env_prefix="TEST",
        profile=profile,
        device="cpu",
        cache_dir=tmp_path,
    )

    assert restarted.encode(["beta", "alpha"]) == [expected[2], expected[0]]
    assert restarted.persistent_cache_hits == 2
    assert restarted.persistent_cache_misses == 0


def test_corrupt_persistent_vector_is_recomputed_and_repaired(monkeypatch, tmp_path) -> None:
    profile = EmbeddingProfile("test", "test/model", 2, "multi", TEST_REVISION_1)
    values = iter(
        [
            np.asarray([[1.0, 2.0]], dtype=np.float32),
            np.asarray([[3.0, 4.0]], dtype=np.float32),
        ]
    )
    calls = 0

    class FakeModel:
        def encode(self, _texts, **_kwargs):
            nonlocal calls
            calls += 1
            return next(values)

    monkeypatch.setattr(BgeEmbedder, "_load", lambda _self: FakeModel())
    first = BgeEmbedder(
        env_prefix="TEST",
        profile=profile,
        device="cpu",
        cache_dir=tmp_path,
    )
    assert first.encode(["repair me"]) == [[1.0, 2.0]]
    assert first.persistent_cache is not None

    cache_key = first.persistent_cache.key(first.embedding_cache_model_id, "repair me")
    with sqlite3.connect(first.persistent_cache.path) as connection:
        connection.execute(
            "UPDATE embedding_cache SET vector = ?, checksum = ? WHERE cache_key = ?",
            (b"broken", "invalid", cache_key),
        )

    BgeEmbedder._cache.clear()
    repaired = BgeEmbedder(
        env_prefix="TEST",
        profile=profile,
        device="cpu",
        cache_dir=tmp_path,
    )
    assert repaired.encode(["repair me"]) == [[3.0, 4.0]]
    assert calls == 2
    assert repaired.persistent_cache_hits == 0
    assert repaired.persistent_cache_misses == 1

    BgeEmbedder._cache.clear()
    monkeypatch.setattr(
        BgeEmbedder,
        "_load",
        lambda _self: pytest.fail("repaired vector should now be persistent"),
    )
    verified = BgeEmbedder(
        env_prefix="TEST",
        profile=profile,
        device="cpu",
        cache_dir=tmp_path,
    )
    assert verified.encode(["repair me"]) == [[3.0, 4.0]]


def test_embedding_cache_directory_can_be_configured_by_environment(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("LOCAL_EMBEDDING_CACHE_DIR", str(tmp_path))
    embedder = BgeEmbedder(
        env_prefix="LOCAL",
        profile=EmbeddingProfile("test", "test/model", 2, "multi", TEST_REVISION_1),
        device="cpu",
    )

    assert embedder.persistent_cache is not None
    assert embedder.persistent_cache.path.parent == tmp_path.resolve()


def test_builtin_profiles_pin_immutable_model_revisions() -> None:
    profiles = (
        BgeM3Embedder(env_prefix="PINNED", device="cpu").profile,
        BgeSmallZhEmbedder(env_prefix="PINNED", device="cpu").profile,
        BgeSmallEnEmbedder(env_prefix="PINNED", device="cpu").profile,
    )

    assert all(
        profile.revision is not None
        and len(profile.revision) == 40
        and all(character in "0123456789abcdef" for character in profile.revision)
        for profile in profiles
    )


def test_cache_identity_is_structured_and_revision_scoped() -> None:
    first = BgeEmbedder(
        env_prefix="TEST",
        profile=EmbeddingProfile("a:b", "c", 2, "multi", TEST_REVISION_1),
        device="cpu",
    )
    delimiter_collision = BgeEmbedder(
        env_prefix="TEST",
        profile=EmbeddingProfile("a", "b:c", 2, "multi", TEST_REVISION_1),
        device="cpu",
    )
    new_revision = BgeEmbedder(
        env_prefix="TEST",
        profile=EmbeddingProfile("a:b", "c", 2, "multi", TEST_REVISION_2),
        device="cpu",
    )

    assert first.embedding_cache_model_id != delimiter_collision.embedding_cache_model_id
    assert first.embedding_cache_model_id != new_revision.embedding_cache_model_id
    assert first.embedding_model_id != new_revision.embedding_model_id
    assert collection_name("rules", first.profile) != collection_name(
        "rules",
        new_revision.profile,
    )


@pytest.mark.parametrize("bad_payload", ["not a blob", 10])
def test_non_blob_cache_rows_degrade_to_misses(
    monkeypatch,
    tmp_path,
    bad_payload,
) -> None:
    calls = 0

    class FakeModel:
        def encode(self, _texts, **_kwargs):
            nonlocal calls
            calls += 1
            return np.asarray([[1.0, 2.0]], dtype=np.float32)

    monkeypatch.setattr(BgeEmbedder, "_load", lambda _self: FakeModel())
    embedder = BgeEmbedder(
        env_prefix="TEST",
        profile=EmbeddingProfile("test", "test/model", 2, "multi", TEST_REVISION_1),
        device="cpu",
        cache_dir=tmp_path,
    )
    assert embedder.encode(["damaged"]) == [[1.0, 2.0]]
    assert embedder.persistent_cache is not None
    cache_key = embedder.persistent_cache.key(
        embedder.embedding_cache_model_id,
        "damaged",
    )
    with sqlite3.connect(embedder.persistent_cache.path) as connection:
        connection.execute(
            "UPDATE embedding_cache SET vector = ? WHERE cache_key = ?",
            (bad_payload, cache_key),
        )

    BgeEmbedder._cache.clear()
    assert embedder.encode(["damaged"]) == [[1.0, 2.0]]
    assert calls == 2
    assert embedder.persistent_cache_misses == 2


def test_persistent_cache_enforces_entry_and_byte_caps(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    class FakeModel:
        def encode(self, texts, **_kwargs):
            calls.append(list(texts))
            return np.asarray(
                [[float(index), float(len(text))] for index, text in enumerate(texts)],
                dtype=np.float32,
            )

    monkeypatch.setattr(BgeEmbedder, "_load", lambda _self: FakeModel())
    profile = EmbeddingProfile("test", "test/model", 2, "multi", TEST_REVISION_1)
    bounded = BgeEmbedder(
        env_prefix="TEST",
        profile=profile,
        device="cpu",
        cache_dir=tmp_path / "entries",
        cache_max_entries=2,
        cache_max_bytes=10_000,
    )

    bounded.encode(["first", "second", "third"])
    assert bounded.persistent_cache_stats()["entries"] == 2

    BgeEmbedder._cache.clear()
    bounded.encode(["first"])
    assert calls == [["first", "second", "third"], ["first"]]
    assert bounded.persistent_cache_stats()["entries"] == 2

    byte_bounded = BgeEmbedder(
        env_prefix="TEST",
        profile=profile,
        device="cpu",
        cache_dir=tmp_path / "bytes",
        cache_max_entries=10,
        cache_max_bytes=1,
    )
    byte_bounded.encode(["too large"])
    assert byte_bounded.persistent_cache_stats()["entries"] == 0
    assert byte_bounded.persistent_cache_stats()["logical_bytes"] == 0


def test_locked_cache_write_fails_open_quickly(monkeypatch, tmp_path) -> None:
    class FakeModel:
        def encode(self, texts, **_kwargs):
            return np.asarray(
                [[float(len(text)), 1.0] for text in texts],
                dtype=np.float32,
            )

    monkeypatch.setattr(BgeEmbedder, "_load", lambda _self: FakeModel())
    embedder = BgeEmbedder(
        env_prefix="TEST",
        profile=EmbeddingProfile("test", "test/model", 2, "multi", TEST_REVISION_1),
        device="cpu",
        cache_dir=tmp_path,
        cache_busy_timeout_ms=25,
    )
    embedder.encode(["seed"])
    assert embedder.persistent_cache is not None
    BgeEmbedder._cache.clear()

    lock = sqlite3.connect(embedder.persistent_cache.path, timeout=0)
    lock.execute("BEGIN IMMEDIATE")
    started = perf_counter()
    try:
        assert embedder.encode(["new"]) == [[3.0, 1.0]]
    finally:
        elapsed = perf_counter() - started
        lock.rollback()
        lock.close()

    assert elapsed < 0.5
    assert embedder.persistent_cache_errors == 1


@pytest.mark.parametrize("moving_ref", ["main", "dev", "refs/heads/main", "latest"])
def test_embedding_identity_rejects_moving_revisions(moving_ref) -> None:
    with pytest.raises(ValueError, match="40-character commit SHA"):
        BgeEmbedder(
            env_prefix="TEST",
            profile=EmbeddingProfile("test", "test/model", 2, "multi"),
            device="cpu",
            model_revision=moving_ref,
        )


def test_persistent_cache_requires_immutable_revision_and_explicit_dir_wins(
    monkeypatch,
    tmp_path,
) -> None:
    profile = EmbeddingProfile("test", "test/model", 2, "multi")
    with pytest.raises(ValueError, match="40-character commit SHA"):
        BgeEmbedder(
            env_prefix="TEST",
            profile=profile,
            device="cpu",
            cache_dir=tmp_path,
        )

    monkeypatch.setenv("TEST_EMBEDDING_CACHE_DIR", str(tmp_path / "environment"))
    explicit = BgeEmbedder(
        env_prefix="TEST",
        profile=profile,
        device="cpu",
        cache_dir=tmp_path / "explicit",
        model_revision=TEST_REVISION_1,
    )
    assert explicit.persistent_cache is not None
    assert explicit.persistent_cache.path.parent == (tmp_path / "explicit").resolve()


def test_parallel_embedders_share_cache_initialization_lock(monkeypatch, tmp_path) -> None:
    class FakeModel:
        def encode(self, texts, **_kwargs):
            return np.asarray(
                [[float(len(text)), 1.0] for text in texts],
                dtype=np.float32,
            )

    monkeypatch.setattr(BgeEmbedder, "_load", lambda _self: FakeModel())
    embedders = [
        BgeEmbedder(
            env_prefix="TEST",
            profile=EmbeddingProfile(
                "test",
                "test/model",
                2,
                "multi",
                TEST_REVISION_1,
            ),
            device="cpu",
            cache_dir=tmp_path,
            cache_busy_timeout_ms=25,
        )
        for _ in range(16)
    ]

    with ThreadPoolExecutor(max_workers=16) as pool:
        values = list(
            pool.map(
                lambda item: item[0].encode([f"text-{item[1]}"]),
                zip(embedders, range(16), strict=True),
            )
        )

    assert len(values) == 16
    assert all(embedder.persistent_cache_errors == 0 for embedder in embedders)
    assert embedders[0].persistent_cache_stats()["entries"] == 16


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not available")
def test_persistent_cache_restricts_directory_and_database_permissions(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeModel:
        def encode(self, texts, **_kwargs):
            return np.asarray([[float(len(text)), 1.0] for text in texts], dtype=np.float32)

    monkeypatch.setattr(BgeEmbedder, "_load", lambda _self: FakeModel())
    cache_dir = tmp_path / "private-cache"
    embedder = BgeEmbedder(
        env_prefix="TEST",
        profile=EmbeddingProfile(
            "test",
            "test/model",
            2,
            "multi",
            TEST_REVISION_1,
        ),
        device="cpu",
        cache_dir=cache_dir,
    )

    embedder.encode(["sensitive"])
    assert embedder.persistent_cache is not None
    assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(embedder.persistent_cache.path.stat().st_mode) == 0o600
