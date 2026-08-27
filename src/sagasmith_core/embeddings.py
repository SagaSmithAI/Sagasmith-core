"""Configurable, lazily loaded embedding profiles."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from array import array
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from hashlib import sha256
from math import isfinite
from pathlib import Path
from sys import byteorder
from time import time_ns
from typing import ClassVar, Protocol


@dataclass(frozen=True)
class EmbeddingProfile:
    key: str
    model_name: str
    dimensions: int
    language: str
    revision: str | None = None

    @property
    def model_id(self) -> str:
        return f"embedding-{self.key.replace('_', '-')}"

    @property
    def storage_model_id(self) -> str:
        return (
            f"{self.model_name}@{self.revision}"
            if self.revision
            else self.model_name
        )


BGE_M3_PROFILE = EmbeddingProfile(
    "bge_m3",
    "BAAI/bge-m3",
    1024,
    "multi",
    "5617a9f61b028005a4858fdac845db406aefb181",
)
BGE_SMALL_ZH_PROFILE = EmbeddingProfile(
    "bge_small_zh_v1_5",
    "BAAI/bge-small-zh-v1.5",
    512,
    "zh",
    "7999e1d3359715c523056ef9478215996d62a620",
)
BGE_SMALL_EN_PROFILE = EmbeddingProfile(
    "bge_small_en_v1_5",
    "BAAI/bge-small-en-v1.5",
    384,
    "en",
    "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
)
EMBEDDING_PROFILES = {
    profile.key: profile for profile in (BGE_M3_PROFILE, BGE_SMALL_ZH_PROFILE, BGE_SMALL_EN_PROFILE)
}
_ALIASES = {
    "m3": "bge_m3",
    "bge-m3": "bge_m3",
    "zh": "bge_small_zh_v1_5",
    "small-zh": "bge_small_zh_v1_5",
    "en": "bge_small_en_v1_5",
    "small-en": "bge_small_en_v1_5",
}
_EMBEDDING_CACHE_EPOCH = "normalized-float32-v1"
_IMMUTABLE_MODEL_REVISION = re.compile(r"^[0-9a-fA-F]{40}$")


def _configured_int(
    env_name: str,
    explicit: int | None,
    default: int,
) -> int:
    value = explicit if explicit is not None else int(os.environ.get(env_name, str(default)))
    return int(value)


def configured_profiles(env_prefix: str) -> tuple[EmbeddingProfile, ...]:
    prefix = env_prefix.upper()
    raw = os.environ.get(f"{prefix}_EMBEDDING_PROFILES", "bge_m3")
    keys: list[str] = []
    for item in raw.split(","):
        value = item.strip().lower()
        if not value:
            continue
        key = _ALIASES.get(value, value)
        if key not in EMBEDDING_PROFILES:
            choices = ", ".join(EMBEDDING_PROFILES)
            raise ValueError(
                f"unknown {prefix}_EMBEDDING_PROFILES entry {item!r}; choose from {choices}"
            )
        if key not in keys:
            keys.append(key)
    if not keys:
        raise ValueError(f"{prefix}_EMBEDDING_PROFILES must enable at least one model")
    return tuple(EMBEDDING_PROFILES[key] for key in keys)


def normalize_language(language: str | None) -> str:
    value = (language or "").strip().lower().replace("_", "-")
    if value.startswith(("zh", "cn")):
        return "zh"
    if value.startswith("en"):
        return "en"
    return "mixed"


def detect_text_language(text: str) -> str:
    cjk_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    if cjk_count and latin_count:
        smaller = min(cjk_count, latin_count)
        larger = max(cjk_count, latin_count)
        if smaller / larger >= 0.15:
            return "mixed"
    if cjk_count:
        return "zh"
    if latin_count:
        return "en"
    return "mixed"


def profile_for_language(
    language: str | None,
    *,
    env_prefix: str,
) -> EmbeddingProfile:
    profiles = configured_profiles(env_prefix)
    if len(profiles) == 1:
        return profiles[0]
    normalized = normalize_language(language)
    matching = [p for p in profiles if p.language in {normalized, "multi"}]
    language_specific = [p for p in matching if p.language == normalized]
    return (language_specific or matching or list(profiles))[0]


def cuda_available() -> bool:
    try:
        import torch
    except (ImportError, RuntimeError):
        return False
    return bool(torch.cuda.is_available())


def embedding_device(env_prefix: str) -> str:
    prefix = env_prefix.upper()
    if configured := os.environ.get(f"{prefix}_EMBEDDING_DEVICE"):
        return configured
    mode = os.environ.get(f"{prefix}_EMBEDDING_MODE", "auto").casefold()
    if mode not in {"auto", "cpu", "gpu"}:
        raise ValueError(f"{prefix}_EMBEDDING_MODE must be auto, cpu, or gpu")
    if mode == "gpu":
        if not cuda_available():
            raise RuntimeError(f"{prefix}_EMBEDDING_MODE=gpu but CUDA is unavailable")
        return "cuda"
    if mode == "auto" and cuda_available():
        return "cuda"
    return "cpu"


def collection_name(base_name: str, profile: EmbeddingProfile) -> str:
    identity = json.dumps(
        {
            "dimensions": profile.dimensions,
            "model_name": profile.model_name,
            "revision": profile.revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    revision_scope = sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{base_name}__{profile.key}__{revision_scope}"


class Embedder(Protocol):
    model_name: str
    dimensions: int
    profile: EmbeddingProfile
    model_id: str

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


def embedding_model_identity(embedder: Embedder) -> str:
    """Return the revision-scoped storage id with legacy embedder fallback."""

    return str(getattr(embedder, "embedding_model_id", embedder.model_name))


class _PersistentEmbeddingCache:
    """Small process-safe SQLite cache for normalized float32 vectors.

    Cache keys contain the model identity and exact encoded text, while the
    database stores only the digest. A damaged row is treated as a miss and is
    replaced after the model recomputes it.
    """

    _SCHEMA = 2
    _QUERY_CHUNK = 400
    _path_locks: ClassVar[dict[Path, threading.RLock]] = {}
    _path_locks_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        max_entries: int,
        max_bytes: int,
        busy_timeout_ms: int,
    ) -> None:
        if max_entries < 1:
            raise ValueError("embedding cache max_entries must be positive")
        if max_bytes < 1:
            raise ValueError("embedding cache max_bytes must be positive")
        if not 0 <= busy_timeout_ms <= 1_000:
            raise ValueError("embedding cache busy_timeout_ms must be between 0 and 1000")
        root = Path(cache_dir).expanduser().resolve(strict=False)
        self.path = root / f"embeddings-v{self._SCHEMA}.sqlite3"
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.busy_timeout_ms = busy_timeout_ms
        self._initialized = False
        with self._path_locks_guard:
            self._database_lock = self._path_locks.setdefault(
                self.path,
                threading.RLock(),
            )

    @classmethod
    def key(cls, model_id: str, text: str) -> str:
        identity = json.dumps(
            [cls._SCHEMA, model_id, text],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _encode_vector(vector: Sequence[float]) -> tuple[bytes, str]:
        packed = array("f", (float(value) for value in vector))
        if byteorder != "little":
            packed.byteswap()
        payload = packed.tobytes()
        return payload, sha256(payload).hexdigest()

    @staticmethod
    def _decode_vector(payload: object, checksum: object, dimensions: int) -> list[float] | None:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            return None
        raw = bytes(payload)
        if len(raw) != dimensions * 4 or sha256(raw).hexdigest() != str(checksum):
            return None
        packed = array("f")
        packed.frombytes(raw)
        if byteorder != "little":
            packed.byteswap()
        vector = list(packed)
        if len(vector) != dimensions or not all(isfinite(value) for value in vector):
            return None
        return vector

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.path.parent.chmod(0o700)
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            if not self._initialized:
                with self._database_lock:
                    if not self._initialized:
                        current_mode = str(
                            connection.execute("PRAGMA journal_mode").fetchone()[0]
                        ).casefold()
                        if current_mode != "wal":
                            connection.execute("PRAGMA journal_mode = WAL")
                        connection.execute(
                            """
                            CREATE TABLE IF NOT EXISTS embedding_cache (
                                cache_key TEXT PRIMARY KEY,
                                model_id TEXT NOT NULL,
                                dimensions INTEGER NOT NULL,
                                vector BLOB NOT NULL,
                                checksum TEXT NOT NULL,
                                logical_bytes INTEGER NOT NULL,
                                written_at INTEGER NOT NULL
                            )
                            """
                        )
                        connection.commit()
                        self._initialized = True
            if os.name != "nt":
                for candidate in (
                    self.path,
                    Path(f"{self.path}-wal"),
                    Path(f"{self.path}-shm"),
                ):
                    if candidate.exists():
                        candidate.chmod(0o600)
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _delete_keys(connection: sqlite3.Connection, keys: Sequence[str]) -> None:
        for start in range(0, len(keys), _PersistentEmbeddingCache._QUERY_CHUNK):
            chunk = list(keys[start : start + _PersistentEmbeddingCache._QUERY_CHUNK])
            placeholders = ",".join("?" for _ in chunk)
            connection.execute(
                f"DELETE FROM embedding_cache WHERE cache_key IN ({placeholders})",
                chunk,
            )

    def _prune(self, connection: sqlite3.Connection) -> int:
        count, logical_bytes = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(logical_bytes), 0) FROM embedding_cache"
        ).fetchone()
        excess_entries = max(0, int(count) - self.max_entries)
        excess_bytes = max(0, int(logical_bytes) - self.max_bytes)
        if not excess_entries and not excess_bytes:
            return 0
        remove: list[str] = []
        removed_bytes = 0
        for cache_key, row_bytes in connection.execute(
            """
            SELECT cache_key, logical_bytes
            FROM embedding_cache
            ORDER BY written_at ASC, cache_key ASC
            """
        ):
            if len(remove) >= excess_entries and removed_bytes >= excess_bytes:
                break
            remove.append(str(cache_key))
            removed_bytes += int(row_bytes)
        self._delete_keys(connection, remove)
        return len(remove)

    def stats(self) -> dict[str, int | str]:
        with closing(self._connect()) as connection:
            count, logical_bytes = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(logical_bytes), 0) FROM embedding_cache"
            ).fetchone()
        return {
            "path": str(self.path),
            "entries": int(count),
            "logical_bytes": int(logical_bytes),
            "max_entries": self.max_entries,
            "max_bytes": self.max_bytes,
        }

    def get_many(
        self,
        keys: Sequence[str],
        *,
        model_id: str,
        dimensions: int,
    ) -> dict[str, list[float]]:
        if not keys:
            return {}
        found: dict[str, list[float]] = {}
        with closing(self._connect()) as connection:
            for start in range(0, len(keys), self._QUERY_CHUNK):
                chunk = list(keys[start : start + self._QUERY_CHUNK])
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT cache_key, vector, checksum
                    FROM embedding_cache
                    WHERE model_id = ? AND dimensions = ?
                      AND cache_key IN ({placeholders})
                    """,
                    [model_id, dimensions, *chunk],
                )
                for cache_key, payload, checksum in rows:
                    vector = self._decode_vector(payload, checksum, dimensions)
                    if vector is not None:
                        found[str(cache_key)] = vector
        return found

    def put_many(
        self,
        values: Sequence[tuple[str, Sequence[float]]],
        *,
        model_id: str,
        dimensions: int,
    ) -> None:
        rows = []
        written_at = time_ns()
        for index, (cache_key, vector) in enumerate(values):
            if len(vector) != dimensions or not all(isfinite(float(value)) for value in vector):
                raise ValueError("embedding vector does not match the configured profile")
            payload, checksum = self._encode_vector(vector)
            logical_bytes = (
                len(payload)
                + len(cache_key.encode("utf-8"))
                + len(model_id.encode("utf-8"))
                + len(checksum)
                + 64
            )
            rows.append(
                (
                    cache_key,
                    model_id,
                    dimensions,
                    payload,
                    checksum,
                    logical_bytes,
                    written_at + index,
                )
            )
        if not rows:
            return
        # SQLite serializes writers across processes. Serialize writers that
        # this process owns as well, so they wait on the path lock instead of
        # consuming the short external-lock busy timeout and failing open.
        with self._database_lock, closing(self._connect()) as connection:
            connection.executemany(
                """
                INSERT INTO embedding_cache (
                    cache_key, model_id, dimensions, vector, checksum,
                    logical_bytes, written_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    model_id = excluded.model_id,
                    dimensions = excluded.dimensions,
                    vector = excluded.vector,
                    checksum = excluded.checksum,
                    logical_bytes = excluded.logical_bytes,
                    written_at = excluded.written_at
                """,
                rows,
            )
            self._prune(connection)
            connection.commit()


class BgeEmbedder:
    """Load sentence-transformers only when dense encoding is requested."""

    _models: ClassVar[dict[tuple[str, str | None, str], object]] = {}
    _model_lock: ClassVar[threading.Lock] = threading.Lock()
    _cache: ClassVar[OrderedDict[tuple[str, str], list[float]]] = OrderedDict()
    _cache_lock: ClassVar[threading.Lock] = threading.Lock()
    _cache_size = 256

    def __init__(
        self,
        *,
        env_prefix: str,
        profile: EmbeddingProfile | None = None,
        language: str | None = None,
        device: str | None = None,
        batch_size: int | None = None,
        show_progress: bool = False,
        cache_dir: str | Path | None = None,
        model_revision: str | None = None,
        cache_epoch: str | None = None,
        cache_max_entries: int | None = None,
        cache_max_bytes: int | None = None,
        cache_busy_timeout_ms: int | None = None,
    ) -> None:
        self.env_prefix = env_prefix.upper()
        selected_profile = profile or profile_for_language(
            language,
            env_prefix=self.env_prefix,
        )
        self.model_revision = (
            model_revision
            or os.environ.get(f"{self.env_prefix}_EMBEDDING_MODEL_REVISION")
            or selected_profile.revision
        )
        if not self.model_revision or not _IMMUTABLE_MODEL_REVISION.fullmatch(
            self.model_revision
        ):
            raise ValueError(
                "embedding model_revision must be an immutable 40-character commit SHA"
            )
        self.model_revision = self.model_revision.casefold()
        self.profile = (
            replace(selected_profile, revision=self.model_revision)
            if selected_profile.revision != self.model_revision
            else selected_profile
        )
        self.model_name = self.profile.model_name
        self.dimensions = self.profile.dimensions
        self.model_id = self.profile.model_id
        self.embedding_model_id = self.profile.storage_model_id
        self.cache_epoch = (
            cache_epoch
            or os.environ.get(f"{self.env_prefix}_EMBEDDING_CACHE_EPOCH")
            or _EMBEDDING_CACHE_EPOCH
        )
        identity = json.dumps(
            {
                "cache_epoch": self.cache_epoch,
                "dimensions": self.dimensions,
                "dtype": "float32",
                "model_id": self.model_id,
                "model_name": self.model_name,
                "model_revision": self.model_revision,
                "normalize_embeddings": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.embedding_cache_model_id = sha256(identity.encode("utf-8")).hexdigest()
        self.device = device or embedding_device(self.env_prefix)
        self.batch_size = batch_size or int(
            os.environ.get(f"{self.env_prefix}_EMBEDDING_BATCH_SIZE", "8")
        )
        self.show_progress = show_progress
        configured_cache_dir = cache_dir or os.environ.get(
            f"{self.env_prefix}_EMBEDDING_CACHE_DIR"
        )
        self.persistent_cache = (
            _PersistentEmbeddingCache(
                configured_cache_dir,
                max_entries=_configured_int(
                    f"{self.env_prefix}_EMBEDDING_CACHE_MAX_ENTRIES",
                    cache_max_entries,
                    50_000,
                ),
                max_bytes=_configured_int(
                    f"{self.env_prefix}_EMBEDDING_CACHE_MAX_BYTES",
                    cache_max_bytes,
                    256 * 1024 * 1024,
                ),
                busy_timeout_ms=_configured_int(
                    f"{self.env_prefix}_EMBEDDING_CACHE_BUSY_TIMEOUT_MS",
                    cache_busy_timeout_ms,
                    50,
                ),
            )
            if configured_cache_dir
            else None
        )
        self.persistent_cache_hits = 0
        self.persistent_cache_misses = 0
        self.persistent_cache_errors = 0

    def _load(self):
        key = (self.model_name, self.model_revision, self.device)
        model = self._models.get(key)
        if model is not None:
            return model
        with self._model_lock:
            model = self._models.get(key)
            if model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    raise RuntimeError(
                        "Dense embeddings require `pip install sagasmith-core[embedding]`"
                    ) from exc
                model = SentenceTransformer(
                    self.model_name,
                    device=self.device,
                    revision=self.model_revision,
                )
                dimension = model.get_sentence_embedding_dimension()
                if dimension != self.dimensions:
                    raise RuntimeError(
                        f"{self.model_name} returned {dimension} dimensions; "
                        f"expected {self.dimensions}"
                    )
                self._models[key] = model
        return model

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        normalized = [str(text) for text in texts]
        if not normalized:
            return []
        results: list[list[float] | None] = [None] * len(normalized)
        pending_indexes: dict[str, list[int]] = {}
        persistent_cache_available = self.persistent_cache is not None
        with self._cache_lock:
            for index, text in enumerate(normalized):
                memory_key = (self.embedding_cache_model_id, text)
                cached = self._cache.get(memory_key)
                if cached is None:
                    pending_indexes.setdefault(text, []).append(index)
                else:
                    self._cache.move_to_end(memory_key)
                    results[index] = list(cached)

        if self.persistent_cache is not None and pending_indexes:
            cache_keys = {
                text: self.persistent_cache.key(self.embedding_cache_model_id, text)
                for text in pending_indexes
            }
            try:
                persisted = self.persistent_cache.get_many(
                    list(cache_keys.values()),
                    model_id=self.embedding_cache_model_id,
                    dimensions=self.dimensions,
                )
            except (OSError, sqlite3.Error, ValueError):
                persistent_cache_available = False
                self.persistent_cache_errors += 1
                self.persistent_cache_misses += len(pending_indexes)
            else:
                persistent_hits: list[tuple[str, list[float]]] = []
                for text, cache_key in cache_keys.items():
                    vector = persisted.get(cache_key)
                    if vector is None:
                        self.persistent_cache_misses += 1
                        continue
                    self.persistent_cache_hits += 1
                    for index in pending_indexes.pop(text):
                        results[index] = list(vector)
                    persistent_hits.append((text, vector))
                with self._cache_lock:
                    for text, vector in persistent_hits:
                        self._cache[(self.embedding_cache_model_id, text)] = list(vector)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)

        missing = list(pending_indexes)
        if missing:
            vectors = self._load().encode(
                missing,
                batch_size=self.batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=self.show_progress,
            )
            encoded = [row.astype("float32").tolist() for row in vectors]
            with self._cache_lock:
                for text, vector in zip(missing, encoded, strict=True):
                    for index in pending_indexes[text]:
                        results[index] = list(vector)
                    self._cache[(self.embedding_cache_model_id, text)] = list(vector)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            if self.persistent_cache is not None and persistent_cache_available:
                try:
                    self.persistent_cache.put_many(
                        [
                            (
                                self.persistent_cache.key(
                                    self.embedding_cache_model_id,
                                    text,
                                ),
                                vector,
                            )
                            for text, vector in zip(missing, encoded, strict=True)
                        ],
                        model_id=self.embedding_cache_model_id,
                        dimensions=self.dimensions,
                    )
                except (OSError, sqlite3.Error, ValueError):
                    self.persistent_cache_errors += 1
        return [row for row in results if row is not None]

    def persistent_cache_stats(self) -> dict[str, int | str | bool]:
        """Return bounded cache usage without exposing source text or cache keys."""

        if self.persistent_cache is None:
            return {"enabled": False}
        return {"enabled": True, **self.persistent_cache.stats()}


def create_embedder(
    *,
    env_prefix: str,
    profile_key: str | None = None,
    language: str | None = None,
    **kwargs,
) -> BgeEmbedder:
    profile = None
    if profile_key:
        key = _ALIASES.get(profile_key.casefold(), profile_key.casefold())
        try:
            profile = EMBEDDING_PROFILES[key]
        except KeyError as exc:
            raise ValueError(f"unknown embedding profile {profile_key!r}") from exc
    return BgeEmbedder(
        env_prefix=env_prefix,
        profile=profile,
        language=language,
        **kwargs,
    )


class BgeM3Embedder(BgeEmbedder):
    def __init__(self, *, env_prefix: str = "TTRPG", **kwargs) -> None:
        super().__init__(env_prefix=env_prefix, profile=BGE_M3_PROFILE, **kwargs)


class BgeSmallZhEmbedder(BgeEmbedder):
    def __init__(self, *, env_prefix: str = "TTRPG", **kwargs) -> None:
        super().__init__(env_prefix=env_prefix, profile=BGE_SMALL_ZH_PROFILE, **kwargs)


class BgeSmallEnEmbedder(BgeEmbedder):
    def __init__(self, *, env_prefix: str = "TTRPG", **kwargs) -> None:
        super().__init__(env_prefix=env_prefix, profile=BGE_SMALL_EN_PROFILE, **kwargs)
