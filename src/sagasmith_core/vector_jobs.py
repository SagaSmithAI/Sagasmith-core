"""Transactional outbox delivery for the optional vector index."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import select

from sagasmith_core.database import Database
from sagasmith_core.models import ModuleChunk, RuleChunk, VectorIndexJob
from sagasmith_core.vector import VectorStore


@dataclass(frozen=True)
class VectorFlushResult:
    attempted: int
    completed: int
    failed: int


class VectorIndexJobService:
    """Deliver committed SQLite embeddings to Chroma with retry-safe upserts."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def flush(
        self,
        vector_store: VectorStore,
        *,
        system_id: str,
        collection: str,
        embedding_model: str,
        profile: Any = None,
        job_ids: Sequence[str] | None = None,
        limit: int = 1_000,
    ) -> VectorFlushResult:
        if limit < 1:
            raise ValueError("vector job limit must be positive")
        if not embedding_model.strip():
            raise ValueError("embedding_model must identify one immutable model revision")
        profile_model = getattr(profile, "storage_model_id", None)
        if profile_model is not None and str(profile_model) != embedding_model:
            raise ValueError("vector profile does not match the requested embedding_model")
        selected_ids = tuple(dict.fromkeys(str(item) for item in job_ids or ()))
        with self.database.transaction() as session:
            statement = (
                select(VectorIndexJob)
                .where(
                    VectorIndexJob.system_id == system_id,
                    VectorIndexJob.collection == collection,
                    VectorIndexJob.operation == "upsert",
                    VectorIndexJob.status.in_(("pending", "failed")),
                    VectorIndexJob.payload["embedding_model"].as_string()
                    == embedding_model,
                )
                .order_by(VectorIndexJob.created_at, VectorIndexJob.id)
                .limit(limit)
            )
            if selected_ids:
                statement = statement.where(VectorIndexJob.id.in_(selected_ids))
            jobs = list(session.scalars(statement))
            deliverable: list[tuple[str, str, list[float], dict[str, Any], str]] = []
            invalid: dict[str, str] = {}
            for job in jobs:
                entity: RuleChunk | ModuleChunk | None
                if job.entity_type == "rule_chunk":
                    entity = session.get(RuleChunk, job.entity_id)
                elif job.entity_type == "module_chunk":
                    entity = session.get(ModuleChunk, job.entity_id)
                else:
                    entity = None
                    invalid[job.id] = f"unsupported vector entity type: {job.entity_type}"
                if entity is None:
                    invalid.setdefault(job.id, "vector entity no longer exists")
                    continue
                embedding = list(entity.embedding_json or [])
                if not embedding:
                    invalid[job.id] = "vector entity has no stored embedding"
                    continue
                payload = dict(job.payload or {})
                deliverable.append(
                    (
                        job.id,
                        job.entity_id,
                        embedding,
                        dict(payload.get("metadata") or {}),
                        str(payload.get("document") or entity.content),
                    )
                )
            for job in jobs:
                if job.id in invalid:
                    job.status = "failed"
                    job.attempts += 1
                    job.error = invalid[job.id]

        delivered_ids: list[str] = []
        delivery_error = ""
        if deliverable:
            try:
                vector_store.upsert(
                    collection,
                    ids=[item[1] for item in deliverable],
                    embeddings=[item[2] for item in deliverable],
                    metadatas=[item[3] for item in deliverable],
                    documents=[item[4] for item in deliverable],
                    profile=profile,
                )
            except Exception as exc:
                delivery_error = str(exc)
            else:
                delivered_ids = [item[0] for item in deliverable]

        deliverable_ids = [item[0] for item in deliverable]
        if deliverable_ids:
            with self.database.transaction() as session:
                for job in session.scalars(
                    select(VectorIndexJob).where(VectorIndexJob.id.in_(deliverable_ids))
                ):
                    job.attempts += 1
                    if job.id in delivered_ids:
                        job.status = "completed"
                        job.error = ""
                    else:
                        job.status = "failed"
                        job.error = delivery_error or "vector delivery failed"
        return VectorFlushResult(
            attempted=len(jobs),
            completed=len(delivered_ids),
            failed=len(jobs) - len(delivered_ids),
        )
