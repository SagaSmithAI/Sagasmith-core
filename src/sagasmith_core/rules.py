"""Rule parsing, ingestion, expansion, and hybrid retrieval."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from sagasmith_core.database import Database
from sagasmith_core.documents import (
    GENERIC_DOCUMENT_LAYOUT_PROFILE,
    DocumentLayoutProfile,
    NormalizedDocument,
    OcrProvider,
    PageLocator,
    apply_document_page_revisions,
    normalize_document,
    strip_page_markers,
)
from sagasmith_core.embeddings import Embedder
from sagasmith_core.idempotency import IdempotencyService, IdempotencyWrite
from sagasmith_core.integrity import unique_retired_source_key
from sagasmith_core.models import RuleChunk, RuleSection, RuleSource, VectorIndexJob
from sagasmith_core.parsing import MarkdownHierarchyParser
from sagasmith_core.portable import (
    portable_rule_chunk_key,
    validate_portable_rule_source,
)
from sagasmith_core.retrieval import (
    SearchHit,
    cosine_similarity,
    enrich_query,
    fts5_hits,
    reciprocal_rank_fusion,
    structured_score,
)
from sagasmith_core.vector import VectorStore
from sagasmith_core.vector_jobs import VectorIndexJobService


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
        idempotency_campaign_id: str | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> RuleIngestResult:
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        source_metadata = {
            **dict(metadata or {}),
            "logical_source_key": source_key,
        }
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
        source_metadata["logical_source_key"] = source_key
        # Activation is authoritative relational state, not caller-controlled
        # source metadata.  Strip the former compatibility shadow.
        source_metadata.pop("import_state", None)
        with self.database.transaction() as session:
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(
                session,
                idempotency_key,
                idempotency_write,
            )
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
                existing.active = True
                existing.metadata_json = source_metadata
                session.flush()
                chunk_count = session.query(RuleChunk).filter_by(source_id=existing.id).count()
                section_count = session.query(RuleSection).filter_by(source_id=existing.id).count()
                result = RuleIngestResult(
                    existing.id,
                    True,
                    section_count,
                    chunk_count,
                    0,
                )
                idempotency.remember_write_in_session(
                    session,
                    campaign_id=idempotency_campaign_id,
                    key=idempotency_key,
                    write=idempotency_write,
                    result={
                        "result": result,
                        "source_metadata": source_metadata,
                    },
                )
                return result
            if existing:
                existing.active = False
                existing.source_key = self._retired_source_key(
                    session,
                    system_id,
                    source_key,
                    existing.checksum,
                )
                existing.metadata_json = {
                    **dict(existing.metadata_json or {}),
                    "logical_source_key": source_key,
                }
                session.flush()

            parsed = (parser or MarkdownHierarchyParser()).parse(content)
            page_locator = PageLocator(content)
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
                    active=True,
                    metadata_json=source_metadata,
                )
            )
            session.flush()
            section_ids: dict[tuple[str, ...], str] = {}
            embedding_count = 0
            chunk_count = 0
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
                                "page_start": page_locator.page_for_offset(
                                    chunk.start_offset
                                ),
                                "page_end": page_locator.page_for_offset(
                                    max(chunk.start_offset, chunk.end_offset - 1)
                                ),
                            },
                        )
                    )
                    chunk_count += 1
                    embedding_count += int(vector is not None)
                    if vector is not None:
                        job_id = str(uuid.uuid4())
                        vector_metadata = {
                            "system_id": system_id,
                            "edition": edition,
                            "locale": locale,
                            "publication_id": publication_id,
                            "source_id": source_id,
                            "section_id": section_id,
                        }
                        session.add(
                            VectorIndexJob(
                                id=job_id,
                                system_id=system_id,
                                collection="rules",
                                entity_type="rule_chunk",
                                entity_id=chunk_id,
                                payload={
                                    "document": chunk_text,
                                    "metadata": vector_metadata,
                                    "embedding_model": embedder.model_name,
                                },
                            )
                        )
            result = RuleIngestResult(
                source_id,
                False,
                len(parsed),
                chunk_count,
                embedding_count,
            )
            idempotency.remember_write_in_session(
                session,
                campaign_id=idempotency_campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result={
                    "result": result,
                    "source_metadata": source_metadata,
                },
            )
            return result

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
        ocr_provider: OcrProvider | None = None,
        document_cache_dir: str | Path | None = None,
        expected_checksum: str | None = None,
        idempotency_campaign_id: str | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
        layout_profile: DocumentLayoutProfile = GENERIC_DOCUMENT_LAYOUT_PROFILE,
        page_revisions: Sequence[Mapping[str, Any]] | None = None,
    ) -> RuleIngestResult:
        """Normalize and ingest a rule document through the shared document pipeline."""
        source_path = Path(path).expanduser().resolve()
        document = normalize_document(
            source_path,
            ocr_provider=ocr_provider,
            cache_dir=document_cache_dir,
            expected_checksum=expected_checksum,
            layout_profile=layout_profile,
        )
        document = apply_document_page_revisions(document, page_revisions)
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
            idempotency_campaign_id=idempotency_campaign_id,
            idempotency_key=idempotency_key,
            idempotency_write=idempotency_write,
        )

    def inspect_path(
        self,
        path: str | Path,
        *,
        parser: MarkdownHierarchyParser | None = None,
        ocr_provider: OcrProvider | None = None,
        document_cache_dir: str | Path | None = None,
        expected_checksum: str | None = None,
        layout_profile: DocumentLayoutProfile = GENERIC_DOCUMENT_LAYOUT_PROFILE,
        page_revisions: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Normalize a rule document without writing it to the rule index."""
        document = normalize_document(
            path,
            ocr_provider=ocr_provider,
            cache_dir=document_cache_dir,
            expected_checksum=expected_checksum,
            layout_profile=layout_profile,
        )
        document = apply_document_page_revisions(document, page_revisions)
        parsed = (parser or MarkdownHierarchyParser()).parse(document.content)
        page_locator = PageLocator(document.content)
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
                    "page_start": page_locator.page_for_offset(section.start_offset),
                    "page_end": page_locator.page_for_offset(
                        max(section.start_offset, section.end_offset - 1)
                    ),
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
        source_ids: list[str] | None = None,
        source_keys: list[str] | None = None,
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
            if not source_ids:
                statement = statement.where(RuleSource.active.is_(True))
            if edition is not None:
                statement = statement.where(RuleSource.edition == edition)
            if locale is not None:
                statement = statement.where(RuleSource.locale == locale)
            if publications:
                statement = statement.where(RuleSource.publication_id.in_(publications))
            if source_ids:
                statement = statement.where(RuleSource.id.in_(source_ids))
            if source_keys:
                statement = statement.where(RuleSource.source_key.in_(source_keys))
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
                session,
                "rule_fts",
                enriched,
                limit=max(top_k * 4, 20),
                weights=(
                    0.0,  # chunk_id UNINDEXED
                    5.0,  # source_title
                    5.0,  # section_title
                    3.0,  # heading_path
                    1.0,  # content
                ),
            )
            if fts_ids:
                # Prune to rows that match the filter criteria
                fts_filtered = [
                    chunk_id
                    for chunk_id in fts_ids
                    if chunk_id in {row.RuleChunk.id for row in rows}
                ]
                fts_ids = fts_filtered

        if fts_ids:
            lexical = fts_ids
        else:
            # Fallback: Python-side structured_score when FTS5 unavailable
            lexical = [
                row.RuleChunk.id
                for row in sorted(
                    rows,
                    key=lambda row: (
                        -structured_score(
                            enriched,
                            section_title=row.RuleSection.title,
                            source_title=row.RuleSource.title,
                            heading_paths=" ".join(row.RuleChunk.heading_path or []),
                            content=row.RuleChunk.content,
                        )
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
                VectorIndexJobService(self.database).flush(
                    vector_store,
                    system_id=system_id,
                    collection="rules",
                    profile=getattr(embedder, "profile", None),
                )
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
                        "source_key": self._logical_source_key(row.RuleSource),
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
                    "key": self._logical_source_key(row.RuleSource),
                    "storage_key": row.RuleSource.source_key,
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
                "source_key": self._logical_source_key(row),
                "storage_key": row.source_key,
                "title": row.title,
                "edition": row.edition,
                "locale": row.locale,
                "version": row.version,
                "publication_id": row.publication_id,
                "authority": row.authority,
                "checksum": row.checksum,
                "active": row.active,
                "metadata": dict(row.metadata_json or {}),
            }

    def source_chunks(self, source_id: str) -> list[dict[str, Any]]:
        """Return deterministic source chunks for a reviewable content extractor."""
        with self.database.transaction() as session:
            source = session.get(RuleSource, source_id)
            if source is None:
                raise LookupError(source_id)
            rows = session.execute(
                select(
                    RuleChunk,
                    RuleSection.ordinal.label("section_ordinal"),
                )
                .join(RuleSection, RuleSection.id == RuleChunk.section_id)
                .where(RuleChunk.source_id == source_id)
                .order_by(
                    RuleSection.ordinal,
                    RuleChunk.ordinal,
                    RuleChunk.id,
                )
            ).all()
            return [
                {
                    "id": row.RuleChunk.id,
                    "section_ordinal": row.section_ordinal,
                    "ordinal": row.RuleChunk.ordinal,
                    "heading_path": list(row.RuleChunk.heading_path),
                    "content": row.RuleChunk.content,
                    "page_start": dict(row.RuleChunk.metadata_json or {}).get("page_start"),
                    "page_end": dict(row.RuleChunk.metadata_json or {}).get("page_end"),
                }
                for row in rows
            ]

    def export_portable_source(self, source_id: str) -> dict[str, Any]:
        """Export one indexed source without leaking local row identifiers."""

        with self.database.transaction() as session:
            source = session.get(RuleSource, source_id)
            if source is None:
                raise LookupError(source_id)
            rows = list(
                session.scalars(
                    select(RuleSection)
                    .where(RuleSection.source_id == source_id)
                    .order_by(RuleSection.ordinal, RuleSection.id)
                )
            )
            parent_ordinals = {row.id: row.ordinal for row in rows}
            chunks_by_section: dict[str, list[RuleChunk]] = {}
            for chunk in session.scalars(
                select(RuleChunk)
                .where(RuleChunk.source_id == source_id)
                .order_by(RuleChunk.ordinal, RuleChunk.id)
            ):
                chunks_by_section.setdefault(chunk.section_id, []).append(chunk)
            canonical_source_key = None
            if source.canonical_source_id:
                canonical = session.get(RuleSource, source.canonical_source_id)
                if canonical is not None:
                    canonical_source_key = self._logical_source_key(canonical)
            source_key = self._logical_source_key(source)
            metadata = {
                key: value
                for key, value in dict(source.metadata_json or {}).items()
                if key not in {"logical_source_key", "source_path"}
            }
            sections = []
            for row in rows:
                chunks = []
                for chunk in chunks_by_section.get(row.id, []):
                    content_hash = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
                    chunks.append(
                        {
                            "key": portable_rule_chunk_key(
                                source_key,
                                row.ordinal,
                                chunk.ordinal,
                                chunk.content,
                            ),
                            "ordinal": chunk.ordinal,
                            "heading_path": list(chunk.heading_path),
                            "content": chunk.content,
                            "content_hash": content_hash,
                            "token_count": chunk.token_count,
                            "metadata": {
                                key: value
                                for key, value in dict(chunk.metadata_json or {}).items()
                                if key not in {"source_id", "section_id"}
                            },
                        }
                    )
                sections.append(
                    {
                        "ordinal": row.ordinal,
                        "parent_ordinal": parent_ordinals.get(row.parent_id),
                        "level": row.level,
                        "title": row.title,
                        "path": list(row.path),
                        "content": row.content,
                        "content_hash": hashlib.sha256(row.content.encode("utf-8")).hexdigest(),
                        "start_offset": row.start_offset,
                        "end_offset": row.end_offset,
                        "chunks": chunks,
                    }
                )
            return validate_portable_rule_source(
                {
                    "source_key": source_key,
                    "title": source.title,
                    "edition": source.edition,
                    "locale": source.locale,
                    "version": source.version,
                    "publication_id": source.publication_id,
                    "authority": source.authority,
                    "canonical_source_key": canonical_source_key,
                    "checksum": source.checksum,
                    "metadata": metadata,
                    "sections": sections,
                }
            )

    def import_portable_source(
        self,
        value: dict[str, Any],
        *,
        system_id: str,
        canonical_source_id: str | None = None,
        embedder: Embedder | None = None,
    ) -> dict[str, Any]:
        """Rehydrate one portable source with fresh source, section, and chunk ids."""

        source_value = validate_portable_rule_source(value)
        source_key = source_value["source_key"]
        checksum = source_value["checksum"]
        if source_value["canonical_source_key"] and not canonical_source_id:
            raise ValueError("portable canonical_source_key must be resolved before source import")
        if canonical_source_id and not source_value["canonical_source_key"]:
            raise ValueError("canonical_source_id requires portable canonical_source_key evidence")
        with self.database.transaction() as session:
            existing = session.scalar(
                select(RuleSource).where(
                    RuleSource.system_id == system_id,
                    RuleSource.source_key == source_key,
                )
            )
            if existing is not None and existing.checksum == checksum:
                existing_id = existing.id
            else:
                if existing is not None:
                    existing.active = False
                    existing.source_key = self._retired_source_key(
                        session,
                        system_id,
                        source_key,
                        existing.checksum,
                    )
                    existing.metadata_json = {
                        **dict(existing.metadata_json or {}),
                        "logical_source_key": source_key,
                    }
                    session.flush()
                existing_id = ""

            if existing_id:
                local_sections = list(
                    session.scalars(
                        select(RuleSection)
                        .where(RuleSection.source_id == existing_id)
                        .order_by(RuleSection.ordinal, RuleSection.id)
                    )
                )
                local_chunk_rows = list(
                    session.execute(
                        select(
                            RuleChunk,
                            RuleSection.ordinal.label("section_ordinal"),
                        )
                        .join(RuleSection, RuleSection.id == RuleChunk.section_id)
                        .where(RuleChunk.source_id == existing_id)
                        .order_by(
                            RuleSection.ordinal,
                            RuleChunk.ordinal,
                            RuleChunk.id,
                        )
                    )
                )
                portable_sections = sorted(
                    source_value["sections"], key=lambda item: item["ordinal"]
                )
                expected_chunk_rows = [
                    (section, chunk)
                    for section in portable_sections
                    for chunk in sorted(section["chunks"], key=lambda item: item["ordinal"])
                ]
                local_parent_ordinals = {section.id: section.ordinal for section in local_sections}
                local_metadata = {
                    key: value
                    for key, value in dict(existing.metadata_json or {}).items()
                    if key not in {"logical_source_key", "source_path"}
                }
                if (
                    existing.title != source_value["title"]
                    or existing.edition != source_value["edition"]
                    or existing.locale != source_value["locale"]
                    or existing.version != source_value["version"]
                    or existing.publication_id != source_value["publication_id"]
                    or existing.authority != source_value["authority"]
                    or existing.canonical_source_id != canonical_source_id
                    or local_metadata != source_value["metadata"]
                    or len(local_sections) != len(portable_sections)
                    or len(local_chunk_rows) != len(expected_chunk_rows)
                    or any(
                        local.ordinal != portable["ordinal"]
                        or local_parent_ordinals.get(local.parent_id) != portable["parent_ordinal"]
                        or local.level != portable["level"]
                        or local.title != portable["title"]
                        or list(local.path) != portable["path"]
                        or local.content != portable["content"]
                        or local.start_offset != portable["start_offset"]
                        or local.end_offset != portable["end_offset"]
                        for local, portable in zip(local_sections, portable_sections, strict=True)
                    )
                    or any(
                        local.section_ordinal != portable_section["ordinal"]
                        or local.RuleChunk.ordinal != portable_chunk["ordinal"]
                        or list(local.RuleChunk.heading_path) != portable_chunk["heading_path"]
                        or local.RuleChunk.content != portable_chunk["content"]
                        or local.RuleChunk.token_count != portable_chunk["token_count"]
                        or {
                            key: value
                            for key, value in dict(local.RuleChunk.metadata_json or {}).items()
                            if key not in {"source_id", "section_id"}
                        }
                        != portable_chunk["metadata"]
                        for local, (
                            portable_section,
                            portable_chunk,
                        ) in zip(local_chunk_rows, expected_chunk_rows, strict=True)
                    )
                ):
                    raise ValueError(
                        "an existing rule source has the same checksum but an "
                        "incompatible portable chunk layout"
                    )
                return {
                    "source_id": existing_id,
                    "skipped": True,
                    "sections": len(local_sections),
                    "chunks": len(local_chunk_rows),
                    "embeddings": 0,
                    "chunk_map": {
                        portable_chunk["key"]: local.RuleChunk.id
                        for local, (
                            _portable_section,
                            portable_chunk,
                        ) in zip(local_chunk_rows, expected_chunk_rows, strict=True)
                    },
                }

            source_id = str(uuid.uuid4())
            session.add(
                RuleSource(
                    id=source_id,
                    system_id=system_id,
                    source_key=source_key,
                    title=source_value["title"],
                    locale=source_value["locale"],
                    edition=source_value["edition"],
                    version=source_value["version"],
                    publication_id=source_value["publication_id"],
                    authority=source_value["authority"],
                    canonical_source_id=canonical_source_id,
                    checksum=checksum,
                    active=True,
                    metadata_json={
                        **dict(source_value["metadata"]),
                        "logical_source_key": source_key,
                    },
                )
            )
            session.flush()
            section_ids: dict[int, str] = {}
            chunk_map: dict[str, str] = {}
            embedding_count = 0
            portable_sections = sorted(source_value["sections"], key=lambda item: item["ordinal"])
            for section in portable_sections:
                section_id = str(uuid.uuid4())
                parent_ordinal = section["parent_ordinal"]
                session.add(
                    RuleSection(
                        id=section_id,
                        source_id=source_id,
                        parent_id=(
                            section_ids[parent_ordinal] if parent_ordinal is not None else None
                        ),
                        ordinal=section["ordinal"],
                        level=section["level"],
                        title=section["title"],
                        path=list(section["path"]),
                        content=section["content"],
                        start_offset=section["start_offset"],
                        end_offset=section["end_offset"],
                    )
                )
                section_ids[section["ordinal"]] = section_id
                session.flush()
                portable_chunks = sorted(section["chunks"], key=lambda item: item["ordinal"])
                chunk_texts = [chunk["content"] for chunk in portable_chunks]
                vectors = embedder.encode(chunk_texts) if embedder else [None] * len(chunk_texts)
                for chunk, vector in zip(portable_chunks, vectors, strict=True):
                    chunk_id = str(uuid.uuid4())
                    chunk_map[chunk["key"]] = chunk_id
                    session.add(
                        RuleChunk(
                            id=chunk_id,
                            source_id=source_id,
                            section_id=section_id,
                            ordinal=chunk["ordinal"],
                            heading_path=list(chunk["heading_path"]),
                            content=chunk["content"],
                            token_count=chunk["token_count"],
                            embedding_model=(embedder.model_name if embedder else None),
                            embedding_json=vector,
                            metadata_json=dict(chunk["metadata"]),
                        )
                    )
                    embedding_count += int(vector is not None)
                    if vector is not None:
                        session.add(
                            VectorIndexJob(
                                id=str(uuid.uuid4()),
                                system_id=system_id,
                                collection="rules",
                                entity_type="rule_chunk",
                                entity_id=chunk_id,
                                payload={
                                    "document": chunk["content"],
                                    "metadata": {
                                        "system_id": system_id,
                                        "edition": source_value["edition"],
                                        "locale": source_value["locale"],
                                        "publication_id": source_value["publication_id"],
                                        "source_id": source_id,
                                        "section_id": section_id,
                                    },
                                    "embedding_model": embedder.model_name,
                                },
                            )
                        )
            return {
                "source_id": source_id,
                "skipped": False,
                "sections": len(portable_sections),
                "chunks": len(chunk_map),
                "embeddings": embedding_count,
                "chunk_map": chunk_map,
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
                "source": f"rule-source:{self._logical_source_key(row.RuleSource)}",
                "source_id": row.RuleSource.id,
                "source_key": self._logical_source_key(row.RuleSource),
                "source_checksum": source_metadata.get("source_checksum", row.RuleSource.checksum),
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
        include_retired: bool = False,
    ) -> list[dict[str, Any]]:
        statement = select(RuleSource).where(RuleSource.system_id == system_id)
        if not include_retired:
            statement = statement.where(RuleSource.active.is_(True))
        if edition is not None:
            statement = statement.where(RuleSource.edition == edition)
        statement = statement.order_by(RuleSource.edition, RuleSource.locale, RuleSource.title)
        with self.database.transaction() as session:
            return [
                {
                    "id": row.id,
                    "source_key": self._logical_source_key(row),
                    "storage_key": row.source_key,
                    "title": row.title,
                    "edition": row.edition,
                    "locale": row.locale,
                    "version": row.version,
                    "publication_id": row.publication_id,
                    "authority": row.authority,
                    "canonical_source_id": row.canonical_source_id,
                    "checksum": row.checksum,
                    "active": row.active,
                    "metadata": dict(row.metadata_json or {}),
                }
                for row in session.scalars(statement)
            ]

    @staticmethod
    def _logical_source_key(row: RuleSource) -> str:
        return str(
            dict(row.metadata_json or {}).get("logical_source_key")
            or row.source_key
        )

    @staticmethod
    def _retired_source_key(
        session: Any,
        system_id: str,
        source_key: str,
        checksum: str,
    ) -> str:
        """Return one unique storage key for an immutable retired rule revision."""

        return unique_retired_source_key(
            source_key,
            checksum,
            exists=lambda candidate: bool(
                session.scalar(
                    select(RuleSource.id).where(
                        RuleSource.system_id == system_id,
                        RuleSource.source_key == candidate,
                    )
                )
            ),
        )
