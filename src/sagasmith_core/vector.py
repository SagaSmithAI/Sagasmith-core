"""Lazy, namespaced ChromaDB client."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sagasmith_core.embeddings import EmbeddingProfile, collection_name
from sagasmith_core.paths import data_root


class VectorStore:
    """Manage collections for one system namespace.

    Importing this module does not require ChromaDB. The optional dependency is
    loaded only after a configured collection is accessed.
    """

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace.strip("_")
        self._client: Any = None
        self._collections: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return bool(os.environ.get("CHROMA_DB_URL") or os.environ.get("CHROMA_DB_PATH"))

    def _connect(self):
        if self._client is not None:
            return self._client
        try:
            from chromadb import HttpClient, PersistentClient
            from chromadb.config import Settings
        except ImportError as exc:
            raise RuntimeError(
                "Vector search requires `pip install sagasmith-core[vector]`"
            ) from exc
        settings = Settings(anonymized_telemetry=False)
        if raw_url := os.environ.get("CHROMA_DB_URL"):
            parsed = urlparse(raw_url)
            kwargs = {
                "host": parsed.hostname or raw_url,
                "ssl": parsed.scheme == "https",
                "settings": settings,
            }
            if parsed.port is not None:
                kwargs["port"] = parsed.port
            self._client = HttpClient(**kwargs)
        else:
            raw_path = os.environ.get("CHROMA_DB_PATH")
            path = Path(raw_path).expanduser() if raw_path else data_root() / "chroma_db"
            path.mkdir(parents=True, exist_ok=True)
            self._client = PersistentClient(path=str(path), settings=settings)
        return self._client

    def scoped_name(self, name: str) -> str:
        return name if name.startswith(f"{self.namespace}_") else f"{self.namespace}_{name}"

    def collection(self, name: str):
        scoped = self.scoped_name(name)
        if scoped not in self._collections:
            self._collections[scoped] = self._connect().get_or_create_collection(
                name=scoped,
                metadata={"hnsw:space": "cosine", "sagasmith_system": self.namespace},
            )
        return self._collections[scoped]

    def collection_for(self, name: str, profile: EmbeddingProfile):
        scoped = collection_name(self.scoped_name(name), profile)
        expected = {
            "hnsw:space": "cosine",
            "sagasmith_system": self.namespace,
            "embedding_model": profile.storage_model_id,
            "embedding_revision": profile.revision or "unversioned",
            "embedding_dimensions": profile.dimensions,
            "embedding_language": profile.language,
            "embedding_index_version": 2,
        }
        if scoped not in self._collections:
            collection = self._connect().get_or_create_collection(
                name=scoped,
                metadata=expected,
            )
            metadata = collection.metadata or {}
            for key, value in expected.items():
                if metadata.get(key) != value:
                    raise RuntimeError(
                        f"collection {scoped!r} has incompatible {key}: "
                        f"{metadata.get(key)!r} != {value!r}"
                    )
            self._collections[scoped] = collection
        return self._collections[scoped]

    def collection_stats(self, name: str) -> dict[str, Any]:
        try:
            collection = self.collection(name)
            return {"name": collection.name, "count": collection.count()}
        except Exception as exc:
            return {"name": self.scoped_name(name), "count": None, "error": str(exc)}

    def upsert(
        self,
        name: str,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]] | None = None,
        documents: list[str] | None = None,
        profile: EmbeddingProfile | None = None,
    ) -> None:
        if not ids:
            return
        collection = self.collection_for(name, profile) if profile else self.collection(name)
        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents,
        )

    def query(
        self,
        name: str,
        *,
        query_embedding: list[float],
        limit: int = 20,
        where: dict[str, Any] | None = None,
        profile: EmbeddingProfile | None = None,
    ) -> list[tuple[str, float]]:
        collection = self.collection_for(name, profile) if profile else self.collection(name)
        count = collection.count()
        if count == 0:
            return []
        result = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(limit, count),
            where=where,
            include=["distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            (item_id, 1.0 - float(distance))
            for item_id, distance in zip(ids, distances, strict=True)
        ]

    def delete(
        self,
        name: str,
        *,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        profile: EmbeddingProfile | None = None,
    ) -> None:
        if not ids and where is None:
            raise ValueError("delete requires ids or where")
        collection = self.collection_for(name, profile) if profile else self.collection(name)
        collection.delete(ids=ids, where=where)

    def dispose(self) -> None:
        self._collections.clear()
        self._client = None
