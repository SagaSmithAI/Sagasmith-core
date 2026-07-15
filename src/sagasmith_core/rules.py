"""Rule parsing, ingestion, expansion, and hybrid retrieval."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from sagasmith_core.database import Database
from sagasmith_core.documents import (
    NormalizedDocument,
    converter_for,
    page_for_offset,
    strip_page_markers,
)
from sagasmith_core.embeddings import Embedder
from sagasmith_core.models import RuleChunk, RuleSection, RuleSource, VectorIndexJob
from sagasmith_core.parsing import MarkdownHierarchyParser
from sagasmith_core.retrieval import (
    SearchHit,
    cosine_similarity,
    enrich_query,
    fts5_hits,
    reciprocal_rank_fusion,
    structured_score,
)
from sagasmith_core.vector import VectorStore


@dataclass(frozen=True)
class RuleIngestResult:
    source_id: str
    skipped: bool
    sections: int
    chunks: int
    embeddings: int


class RuleService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ingest(
        self,
        *,
        system_id: str,
        source_key: str,
        title: str,
        content: str,
        locale: str = "en",
        edition: str = "",
        version: str = "",
        publication_id: str = "",
        authority: str = "primary",
        canonical_source_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        parser: MarkdownHierarchyParser | None = None,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
        normalized_document: NormalizedDocument | None = None,
    ) -> RuleIngestResult:
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        parsed = (parser or MarkdownHierarchyParser()).parse(content)
        source_metadata = dict(metadata or {})
        if normalized_document is not None:
            source_metadata = {
                **source_metadata,
                "source_path": normalized_document.source_path,
                "media_type": normalized_document.media_type,
                "source_checksum": normalized_document.checksum,
                "page_count": normalized_document.page_count,
                "warnings": list(normalized_document.warnings),
                **normalized_document.metadata,
            }
        with self.database.transaction() as session:
            existing = session.scalar(
                select(RuleSource).where(
                    RuleSource.system_id == system_id,
                    RuleSource.source_key == source_key,
                )
            )
            if existing and existing.checksum == checksum:
                existing.title = title
                existing.locale = locale
                existing.edition = edition
                existing.version = version
                existing.publication_id = publication_id
                existing.authority = authority
                existing.canonical_source_id = canonical_source_id
                existing.metadata_json = source_metadata
                session.flush()
                chunk_count = session.query(RuleChunk).filter_by(source_id=existing.id).count()
                section_count = session.query(RuleSection).filter_by(source_id=existing.id).count()
                return RuleIngestResult(existing.id, True, section_count, chunk_count, 0)
            if existing:
                session.execute(delete(RuleSource).where(RuleSource.id == existing.id))
                session.flush()

            source_id = str(uuid.uuid4())
            session.add(
                RuleSource(
                    id=source_id,
                    system_id=system_id,
                    source_key=source_key,
                    title=title,
                    locale=locale,
                    edition=edition,
                    version=version,
                    publication_id=publication_id,
                    authority=authority,
                    canonical_source_id=canonical_source_id,
                    checksum=checksum,
                    metadata_json=source_metadata,
                )
            )
            session.flush()
            section_ids: dict[tuple[str, ...], str] = {}
            embedding_count = 0
            chunk_count = 0
            vector_ids: list[str] = []
            vector_values: list[list[float]] = []
            vector_metadata: list[dict[str, Any]] = []
            vector_documents: list[str] = []
            vector_job_ids: list[str] = []
            for section in parsed:
                section_id = str(uuid.uuid4())
                parent_id = section_ids.get(section.path[:-1])
                section_ids[section.path] = section_id
                session.add(
                    RuleSection(
                        id=section_id,
                        source_id=source_id,
                        parent_id=parent_id,
                        ordinal=section.ordinal,
                        level=section.level,
                        title=section.title,
                        path=list(section.path),
                        content=section.content,
                        start_offset=section.start_offset,
                        end_offset=section.end_offset,
                    )
                )
                session.flush()
                chunk_texts = [strip_page_markers(chunk.content) for chunk in section.chunks]
                vectors = embedder.encode(chunk_texts) if embedder else [None] * len(chunk_texts)
                for chunk, chunk_text, vector in zip(
                    section.chunks, chunk_texts, vectors, strict=True
                ):
                    chunk_id = str(uuid.uuid4())
                    session.add(
                        RuleChunk(
                            id=chunk_id,
                            source_id=source_id,
                            section_id=section_id,
                            ordinal=chunk_count,
                            heading_path=list(chunk.heading_path),
                            content=chunk_text,
                            token_count=max(1, len(chunk_text) // 4),
                            embedding_model=embedder.model_name if embedder else None,
                            embedding_json=vector,
                            metadata_json={
                                **chunk.metadata,
                                "start_offset": chunk.start_offset,
                                "end_offset": chunk.end_offset,
                                "page_start": page_for_offset(content, chunk.start_offset),
                                "page_end": page_for_offset(content, chunk.end_offset),
                            },
                        )
                    )
                    chunk_count += 1
                    embedding_count += int(vector is not None)
                    if vector is not None:
                        job_id = str(uuid.uuid4())
                        vector_job_ids.append(job_id)
                        vector_ids.append(chunk_id)
                        vector_values.append(vector)
                        vector_metadata.append(
                            {
                                "system_id": system_id,
                                "edition": edition,
                                "locale": locale,
                                "publication_id": publication_id,
                                "source_id": source_id,
                                "section_id": section_id,
                            }
                        )
                        vector_documents.append(chunk_text)
                        session.add(
                            VectorIndexJob(
                                id=job_id,
                                system_id=system_id,
                                collection="rules",
                                entity_type="rule_chunk",
                                entity_id=chunk_id,
                                payload={
                                    "document": chunk_text,
                                    "metadata": vector_metadata[-1],
                                    "embedding_model": embedder.model_name,
                                },
                            )
                        )
            if vector_store and vector_values:
                try:
                    vector_store.upsert(
                        "rules",
                        ids=vector_ids,
                        embeddings=vector_values,
                        metadatas=vector_metadata,
                        documents=vector_documents,
                        profile=getattr(embedder, "profile", None),
                    )
                except Exception as exc:
                    for job_id in vector_job_ids:
                        job = session.get(VectorIndexJob, job_id)
                        job.status = "failed"
                        job.attempts = 1
                        job.error = str(exc)
                else:
                    for job_id in vector_job_ids:
                        job = session.get(VectorIndexJob, job_id)
                        job.status = "completed"
                        job.attempts = 1
            return RuleIngestResult(
                source_id,
                False,
                len(parsed),
                chunk_count,
                embedding_count,
            )

    def ingest_path(
        self,
        *,
        system_id: str,
        path: str | Path,
        source_key: str | None = None,
        title: str | None = None,
        locale: str = "en",
        edition: str = "",
        version: str = "",
        publication_id: str = "",
        authority: str = "primary",
        canonical_source_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        parser: MarkdownHierarchyParser | None = None,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
    ) -> RuleIngestResult:
        """Normalize and ingest a rule document through the shared document pipeline."""
        source_path = Path(path).expanduser().resolve()
        document = converter_for(source_path).convert(source_path)
        return self.ingest(
            system_id=system_id,
            source_key=source_key or source_path.name,
            title=title or source_path.stem,
            content=document.content,
            locale=locale,
            edition=edition,
            version=version,
            publication_id=publication_id,
            authority=authority,
            canonical_source_id=canonical_source_id,
            metadata=metadata,
            parser=parser,
            embedder=embedder,
            vector_store=vector_store,
            normalized_document=document,
        )

    def inspect_path(
        self,
        path: str | Path,
        *,
        parser: MarkdownHierarchyParser | None = None,
    ) -> dict[str, Any]:
        """Normalize a rule document without writing it to the rule index."""
        document = converter_for(path).convert(path)
        parsed = (parser or MarkdownHierarchyParser()).parse(document.content)
        return {
            "source_path": document.source_path,
            "media_type": document.media_type,
            "checksum": document.checksum,
            "page_count": document.page_count,
            "warnings": list(document.warnings),
            "metadata": dict(document.metadata),
            "sections": len(parsed),
            "chunks": sum(len(section.chunks) for section in parsed),
            "outline": [
                {
                    "title": section.title,
                    "level": section.level,
                    "path": list(section.path),
                    "page_start": page_for_offset(document.content, section.start_offset),
                    "page_end": page_for_offset(document.content, section.end_offset),
                }
                for section in parsed
            ],
        }

    def search(
        self,
        *,
        system_id: str,
        query: str,
        edition: str | None = None,
        locale: str | None = None,
        publications: list[str] | None = None,
        top_k: int = 8,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
        query_hints: dict[str, Sequence[str]] | None = None,
    ) -> list[SearchHit]:
        enriched = enrich_query(query, extra_terms=query_hints)
        with self.database.transaction() as session:
            statement = (
                select(RuleChunk, RuleSection, RuleSource)
                .join(RuleSection, RuleSection.id == RuleChunk.section_id)
                .join(RuleSource, RuleSource.id == RuleChunk.source_id)
                .where(RuleSource.system_id == system_id)
            )
            if edition is not None:
                statement = statement.where(RuleSource.edition == edition)
            if locale is not None:
                statement = statement.where(RuleSource.locale == locale)
            if publications:
                statement = statement.where(RuleSource.publication_id.in_(publications))
            rows = session.execute(statement).all()
        if not rows:
            return []

        exact = [
            row
            for row in rows
            if row.RuleSection.title.casefold() == query.casefold()
            or row.RuleSource.title.casefold() == query.casefold()
        ]
        exact_ids = {row.RuleChunk.id for row in exact}

        # FTS5 lexical channel — indexed BM25 on SQLite, zero deps
        fts_ids: list[str] = []
        with self.database.transaction() as session:
            fts_ids = fts5_hits(
                session, "rule_fts", enriched,
                limit=max(top_k * 4, 20),
                weights=(
                    0.0,   # chunk_id UNINDEXED
                    5.0,   # source_title
                    5.0,   # section_title
                    3.0,   # heading_path
                    1.0,   # content
                ),
            )
            if fts_ids:
                # Prune to rows that match the filter criteria
                fts_filtered = [
                    chunk_id for chunk_id in fts_ids
                    if chunk_id in {row.RuleChunk.id for row in rows}
                ]
                fts_ids = fts_filtered

        if fts_ids:
            lexical = fts_ids
        else:
            # Fallback: Python-side structured_score when FTS5 unavailable
            lexical = [
                row.RuleChunk.id for row in sorted(
                    rows,
                    key=lambda row: -structured_score(
                        enriched,
                        section_title=row.RuleSection.title,
                        source_title=row.RuleSource.title,
                        heading_paths=" ".join(row.RuleChunk.heading_path or []),
                        content=row.RuleChunk.content,
                    ),
                )
            ]

        rankings: dict[str, list[str]] = {
            "exact": list(exact_ids),
            "lexical": lexical,
        }
        if embedder:
            query_vector = embedder.encode([query])[0]
            if vector_store and vector_store.enabled:
                filters: list[dict[str, Any]] = [{"system_id": system_id}]
                if edition is not None:
                    filters.append({"edition": edition})
                if locale is not None:
                    filters.append({"locale": locale})
                where = filters[0] if len(filters) == 1 else {"$and": filters}
                rankings["dense"] = [
                    item_id
                    for item_id, _score in vector_store.query(
                        "rules",
                        query_embedding=query_vector,
                        limit=max(top_k * 4, 20),
                        where=where,
                        profile=getattr(embedder, "profile", None),
                    )
                    if item_id in {row.RuleChunk.id for row in rows}
                ]
            else:
                dense = sorted(
                    (
                        (
                            cosine_similarity(query_vector, row.RuleChunk.embedding_json or []),
                            row,
                        )
                        for row in rows
                        if row.RuleChunk.embedding_model == embedder.model_name
                    ),
                    key=lambda item: -item[0],
                )
                rankings["dense"] = [row.RuleChunk.id for _, row in dense]

        by_id = {row.RuleChunk.id: row for row in rows}
        fused = reciprocal_rank_fusion(
            rankings,
            weights={"exact": 1.5, "lexical": 1.0, "dense": 1.0},
        )
        hits: list[SearchHit] = []
        for chunk_id, score, retrieval in fused[:top_k]:
            row = by_id[chunk_id]
            hits.append(
                SearchHit(
                    id=chunk_id,
                    score=score,
                    title=row.RuleSection.title,
                    content=row.RuleChunk.content,
                    source_id=row.RuleSource.id,
                    heading_path=tuple(row.RuleChunk.heading_path),
                    retrieval=retrieval,
                    metadata={
                        "source_key": row.RuleSource.source_key,
                        "version": row.RuleSource.version,
                        "locale": row.RuleSource.locale,
                        "edition": row.RuleSource.edition,
                        "publication_id": row.RuleSource.publication_id,
                        "authority": row.RuleSource.authority,
                        "canonical_source_id": row.RuleSource.canonical_source_id,
                        "source_checksum": dict(row.RuleSource.metadata_json or {}).get(
                            "source_checksum", row.RuleSource.checksum
                        ),
                        "page_start": dict(row.RuleChunk.metadata_json or {}).get("page_start"),
                        "page_end": dict(row.RuleChunk.metadata_json or {}).get("page_end"),
                    },
                )
            )
        return hits

    def expand(self, chunk_id: str) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = session.execute(
                select(RuleChunk, RuleSection, RuleSource)
                .join(RuleSection, RuleSection.id == RuleChunk.section_id)
                .join(RuleSource, RuleSource.id == RuleChunk.source_id)
                .where(RuleChunk.id == chunk_id)
            ).one()
            return {
                "chunk_id": row.RuleChunk.id,
                "section_id": row.RuleSection.id,
                "title": row.RuleSection.title,
                "path": list(row.RuleSection.path),
                "content": row.RuleSection.content,
                "chunk": {
                    "content": row.RuleChunk.content,
                    "heading_path": list(row.RuleChunk.heading_path),
                    "page_start": dict(row.RuleChunk.metadata_json or {}).get("page_start"),
                    "page_end": dict(row.RuleChunk.metadata_json or {}).get("page_end"),
                },
                "source": {
                    "id": row.RuleSource.id,
                    "key": row.RuleSource.source_key,
                    "title": row.RuleSource.title,
                    "version": row.RuleSource.version,
                    "locale": row.RuleSource.locale,
                    "edition": row.RuleSource.edition,
                    "publication_id": row.RuleSource.publication_id,
                    "authority": row.RuleSource.authority,
                    "canonical_source_id": row.RuleSource.canonical_source_id,
                    "checksum": row.RuleSource.checksum,
                    "metadata": dict(row.RuleSource.metadata_json or {}),
                },
            }

    def source(self, source_id: str) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = session.get(RuleSource, source_id)
            if row is None:
                raise LookupError(source_id)
            return {
                "id": row.id,
                "system_id": row.system_id,
                "source_key": row.source_key,
                "title": row.title,
                "edition": row.edition,
                "locale": row.locale,
                "version": row.version,
                "publication_id": row.publication_id,
                "authority": row.authority,
                "checksum": row.checksum,
                "metadata": dict(row.metadata_json or {}),
            }

    def citation(self, chunk_id: str, *, source_id: str | None = None) -> dict[str, Any]:
        """Resolve a caller-supplied chunk id into canonical, source-bound evidence."""
        with self.database.transaction() as session:
            row = session.execute(
                select(RuleChunk, RuleSource)
                .join(RuleSource, RuleSource.id == RuleChunk.source_id)
                .where(RuleChunk.id == chunk_id)
            ).one_or_none()
            if row is None:
                raise LookupError(chunk_id)
            if source_id is not None and row.RuleSource.id != source_id:
                raise ValueError("rule chunk does not belong to the requested source")
            metadata = dict(row.RuleChunk.metadata_json or {})
            source_metadata = dict(row.RuleSource.metadata_json or {})
            return {
                "source": f"rule-source:{row.RuleSource.source_key}",
                "source_id": row.RuleSource.id,
                "source_key": row.RuleSource.source_key,
                "source_checksum": source_metadata.get(
                    "source_checksum", row.RuleSource.checksum
                ),
                "chunk_id": row.RuleChunk.id,
                "heading_path": list(row.RuleChunk.heading_path),
                "page_start": metadata.get("page_start"),
                "page_end": metadata.get("page_end"),
            }

    def sources(
        self,
        *,
        system_id: str,
        edition: str | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(RuleSource).where(RuleSource.system_id == system_id)
        if edition is not None:
            statement = statement.where(RuleSource.edition == edition)
        statement = statement.order_by(RuleSource.edition, RuleSource.locale, RuleSource.title)
        with self.database.transaction() as session:
            return [
                {
                    "id": row.id,
                    "source_key": row.source_key,
                    "title": row.title,
                    "edition": row.edition,
                    "locale": row.locale,
                    "version": row.version,
                    "publication_id": row.publication_id,
                    "authority": row.authority,
                    "canonical_source_id": row.canonical_source_id,
                    "checksum": row.checksum,
                    "metadata": dict(row.metadata_json or {}),
                }
                for row in session.scalars(statement)
            ]
