"""Configurable, lazily loaded embedding profiles."""

from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol


@dataclass(frozen=True)
class EmbeddingProfile:
    key: str
    model_name: str
    dimensions: int
    language: str

    @property
    def model_id(self) -> str:
        return f"embedding-{self.key.replace('_', '-')}"


BGE_M3_PROFILE = EmbeddingProfile("bge_m3", "BAAI/bge-m3", 1024, "multi")
BGE_SMALL_ZH_PROFILE = EmbeddingProfile("bge_small_zh_v1_5", "BAAI/bge-small-zh-v1.5", 512, "zh")
BGE_SMALL_EN_PROFILE = EmbeddingProfile("bge_small_en_v1_5", "BAAI/bge-small-en-v1.5", 384, "en")
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
    return f"{base_name}__{profile.key}"


class Embedder(Protocol):
    model_name: str
    dimensions: int
    profile: EmbeddingProfile
    model_id: str

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class BgeEmbedder:
    """Load sentence-transformers only when dense encoding is requested."""

    _models: ClassVar[dict[tuple[str, str], object]] = {}
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
    ) -> None:
        self.env_prefix = env_prefix.upper()
        self.profile = profile or profile_for_language(
            language,
            env_prefix=self.env_prefix,
        )
        self.model_name = self.profile.model_name
        self.dimensions = self.profile.dimensions
        self.model_id = self.profile.model_id
        self.device = device or embedding_device(self.env_prefix)
        self.batch_size = batch_size or int(
            os.environ.get(f"{self.env_prefix}_EMBEDDING_BATCH_SIZE", "8")
        )
        self.show_progress = show_progress

    def _load(self):
        key = (self.model_name, self.device)
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
                model = SentenceTransformer(self.model_name, device=self.device)
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
        missing: list[str] = []
        indexes: list[int] = []
        with self._cache_lock:
            for index, text in enumerate(normalized):
                cached = self._cache.get((self.model_name, text))
                if cached is None:
                    missing.append(text)
                    indexes.append(index)
                else:
                    results[index] = list(cached)
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
                for index, text, vector in zip(indexes, missing, encoded, strict=True):
                    results[index] = vector
                    self._cache[(self.model_name, text)] = vector
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        return [row for row in results if row is not None]


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
