"""Adventure-module parsing, ingestion, search, and scene progress."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import delete, select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.documents import (
    NormalizedDocument,
    converter_for,
    page_for_offset,
    strip_page_markers,
)
from sagasmith_core.embeddings import Embedder
from sagasmith_core.models import (
    Campaign,
    ModuleAsset,
    ModuleChapter,
    ModuleChunk,
    ModuleScene,
    ModuleSource,
    SceneProgress,
    VectorIndexJob,
)
from sagasmith_core.parsing import MarkdownHierarchyParser
from sagasmith_core.retrieval import (
    SearchHit,
    cosine_similarity,
    lexical_score,
    reciprocal_rank_fusion,
)
from sagasmith_core.vector import VectorStore


@dataclass(frozen=True)
class ParsedScene:
    ordinal: int
    title: str
    content: str
    heading_path: tuple[str, ...]
    chunks: tuple
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedChapter:
    ordinal: int
    title: str
    content: str
    scenes: tuple[ParsedScene, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


class ModuleStructureProfile(Protocol):
    name: str
    version: str

    def classify_chunk(self, heading: str, text: str) -> str: ...

    def keywords(self, title: str, text: str) -> list[str]: ...


class GenericModuleProfile:
    name = "generic"
    version = "1"

    def classify_chunk(self, heading: str, text: str) -> str:
        lines = [line for line in text.splitlines() if line.strip()]
        if lines and all(line.lstrip().startswith("|") for line in lines):
            return "table"
        if text.lstrip().startswith(">"):
            return "read_aloud"
        if lines and sum(
            line.lstrip().startswith(("-", "*")) for line in lines
        ) >= len(lines) / 2:
            return "list"
        if heading.casefold() in {"appendix", "附录", "reference", "参考"}:
            return "reference"
        return "narrative"

    def keywords(self, title: str, text: str) -> list[str]:
        values = re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}|[\u4e00-\u9fff]{2,8}", title)
        return list(dict.fromkeys(value.casefold() for value in values))[:20]


class MarkdownModuleParser:
    """Interpret H1 as chapters and recover scene-sized H2/H3 boundaries."""

    def __init__(
        self,
        hierarchy_parser: MarkdownHierarchyParser | None = None,
        *,
        profile: ModuleStructureProfile | None = None,
    ) -> None:
        self.hierarchy_parser = hierarchy_parser or MarkdownHierarchyParser()
        self.profile = profile or GenericModuleProfile()

    def parse(self, content: str) -> list[ParsedChapter]:
        heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
        headings = list(heading_re.finditer(content))
        if not headings:
            return [self._chapter(0, "Document", content, 0, len(content))]
        chapter_starts = [
            (index, match)
            for index, match in enumerate(headings)
            if len(match.group(1)) == 1
        ]
        if not chapter_starts:
            chapter_starts = [(0, headings[0])]
        parsed: list[ParsedChapter] = []
        for ordinal, (heading_index, heading) in enumerate(chapter_starts):
            start = heading.start()
            end = (
                chapter_starts[ordinal + 1][1].start()
                if ordinal + 1 < len(chapter_starts)
                else len(content)
            )
            title = heading.group(2).strip()
            parsed.append(self._chapter(ordinal, title, content[start:end], start, end))
        return parsed

    def _chapter(
        self,
        ordinal: int,
        title: str,
        chapter_content: str,
        global_start: int,
        global_end: int,
    ) -> ParsedChapter:
        matches = list(re.finditer(r"^(#{2,4})\s+(.+?)\s*$", chapter_content, re.MULTILINE))
        counts = {
            level: sum(len(match.group(1)) == level for match in matches)
            for level in (2, 3, 4)
        }
        if counts[2] and counts[3] >= counts[2] * 5:
            scene_level = 3
        elif counts[2]:
            scene_level = 2
        elif counts[3]:
            scene_level = 3
        else:
            scene_level = 4
        scene_headings = [
            match for match in matches if len(match.group(1)) == scene_level
        ]
        ranges: list[tuple[str, int, int]] = []
        for index, heading in enumerate(scene_headings):
            end = (
                scene_headings[index + 1].start()
                if index + 1 < len(scene_headings)
                else len(chapter_content)
            )
            ranges.append((heading.group(2).strip(), heading.start(), end))
        if not ranges:
            ranges.append((title, 0, len(chapter_content)))

        scenes: list[ParsedScene] = []
        for scene_ordinal, (scene_title, start, end) in enumerate(ranges):
            raw = chapter_content[start:end].strip()
            clean = strip_page_markers(raw)
            sections = self.hierarchy_parser.parse(raw)
            chunks = []
            for section in sections:
                for chunk in section.chunks:
                    text = strip_page_markers(chunk.content)
                    if not text:
                        continue
                    absolute_start = global_start + start + chunk.start_offset
                    absolute_end = global_start + start + chunk.end_offset
                    metadata = {
                        **chunk.metadata,
                        "start_line": chapter_content.count("\n", 0, start + chunk.start_offset)
                        + 1,
                        "end_line": chapter_content.count("\n", 0, start + chunk.end_offset)
                        + 1,
                        "page_start": page_for_offset(chapter_content, start + chunk.start_offset),
                        "page_end": page_for_offset(chapter_content, start + chunk.end_offset),
                        "chunk_type": self.profile.classify_chunk(section.title, text),
                        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "absolute_start": absolute_start,
                        "absolute_end": absolute_end,
                    }
                    chunks.append(
                        type(chunk)(
                            ordinal=len(chunks),
                            heading_path=(title, *chunk.heading_path),
                            content=text,
                            start_offset=absolute_start,
                            end_offset=absolute_end,
                            metadata=metadata,
                        )
                    )
            scene_start = global_start + start
            scene_end = global_start + end
            scenes.append(
                ParsedScene(
                    ordinal=scene_ordinal,
                    title=scene_title,
                    content=clean,
                    heading_path=(title, scene_title),
                    chunks=tuple(chunks),
                    metadata={
                        "start_line": chapter_content.count("\n", 0, start) + 1,
                        "end_line": chapter_content.count("\n", 0, end) + 1,
                        "page_start": page_for_offset(chapter_content, start),
                        "page_end": page_for_offset(chapter_content, end),
                        "keywords": self.profile.keywords(scene_title, clean),
                        "absolute_start": scene_start,
                        "absolute_end": scene_end,
                    },
                )
            )
        return ParsedChapter(
            ordinal=ordinal,
            title=title,
            content=strip_page_markers(chapter_content),
            scenes=tuple(scenes),
            metadata={
                "page_start": page_for_offset(chapter_content, 0),
                "page_end": page_for_offset(chapter_content, len(chapter_content)),
                "absolute_start": global_start,
                "absolute_end": global_end,
            },
        )


@dataclass(frozen=True)
class ModuleIngestResult:
    module_id: str
    skipped: bool
    chapters: int
    scenes: int
    chunks: int
    embeddings: int


class ModuleService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ingest(
        self,
        *,
        campaign_id: str,
        source_key: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        parser: MarkdownModuleParser | None = None,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
        source_path: str = "",
        normalized_document: NormalizedDocument | None = None,
    ) -> ModuleIngestResult:
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        parsed = (parser or MarkdownModuleParser()).parse(content)
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            existing = session.scalar(
                select(ModuleSource).where(
                    ModuleSource.campaign_id == campaign_id,
                    ModuleSource.source_key == source_key,
                )
            )
            if existing and existing.checksum == checksum:
                counts = self._counts(session, existing.id)
                return ModuleIngestResult(existing.id, True, *counts, 0)
            if existing:
                session.execute(delete(ModuleSource).where(ModuleSource.id == existing.id))
                session.flush()

            module_id = str(uuid.uuid4())
            profile = getattr(parser, "profile", GenericModuleProfile())
            source_row = ModuleSource(
                id=module_id,
                system_id=campaign.system_id,
                campaign_id=campaign_id,
                source_key=source_key,
                title=title,
                source_path=source_path,
                checksum=checksum,
                parser_profile=getattr(profile, "name", "generic"),
                parser_version=getattr(profile, "version", "1"),
                warnings=list(normalized_document.warnings) if normalized_document else [],
                metadata_json=metadata or {},
            )
            session.add(source_row)
            session.flush()
            if normalized_document is not None:
                session.add(
                    ModuleAsset(
                        id=str(uuid.uuid4()),
                        module_id=module_id,
                        source_path=normalized_document.source_path,
                        media_type=normalized_document.media_type,
                        checksum=normalized_document.checksum,
                        normalized_content=normalized_document.content,
                        metadata_json={
                            **normalized_document.metadata,
                            "warnings": list(normalized_document.warnings),
                            "page_count": normalized_document.page_count,
                        },
                    )
                )
            scene_count = 0
            chunk_count = 0
            embedding_count = 0
            vector_ids: list[str] = []
            vector_values: list[list[float]] = []
            vector_metadata: list[dict[str, Any]] = []
            vector_documents: list[str] = []
            vector_job_ids: list[str] = []
            for chapter in parsed:
                chapter_id = str(uuid.uuid4())
                session.add(
                    ModuleChapter(
                        id=chapter_id,
                        module_id=module_id,
                        ordinal=chapter.ordinal,
                        title=chapter.title,
                        content=chapter.content,
                        source_path=source_path,
                        status="current" if chapter.ordinal == 0 else "locked",
                        page_start=chapter.metadata.get("page_start"),
                        page_end=chapter.metadata.get("page_end"),
                        metadata_json=chapter.metadata,
                    )
                )
                session.flush()
                for scene in chapter.scenes:
                    scene_id = str(uuid.uuid4())
                    session.add(
                        ModuleScene(
                            id=scene_id,
                            module_id=module_id,
                            chapter_id=chapter_id,
                            ordinal=scene.ordinal,
                            title=scene.title,
                            content=scene.content,
                            start_line=scene.metadata.get("start_line", 1),
                            end_line=scene.metadata.get("end_line", 1),
                            page_start=scene.metadata.get("page_start"),
                            page_end=scene.metadata.get("page_end"),
                            headings=list(scene.heading_path),
                            keywords=scene.metadata.get("keywords", []),
                            metadata_json=scene.metadata,
                        )
                    )
                    session.flush()
                    texts = [chunk.content for chunk in scene.chunks]
                    vectors = embedder.encode(texts) if embedder else [None] * len(texts)
                    for chunk, vector in zip(scene.chunks, vectors, strict=True):
                        chunk_id = str(uuid.uuid4())
                        session.add(
                            ModuleChunk(
                                id=chunk_id,
                                module_id=module_id,
                                scene_id=scene_id,
                                ordinal=chunk_count,
                                heading_path=list(chunk.heading_path),
                                content=chunk.content,
                                token_count=max(1, len(chunk.content) // 4),
                                start_line=chunk.metadata.get("start_line", 1),
                                end_line=chunk.metadata.get("end_line", 1),
                                char_start=chunk.start_offset,
                                char_end=chunk.end_offset,
                                page_start=chunk.metadata.get("page_start"),
                                page_end=chunk.metadata.get("page_end"),
                                chunk_type=chunk.metadata.get("chunk_type", "narrative"),
                                content_hash=chunk.metadata.get("content_hash", ""),
                                embedding_model=embedder.model_name if embedder else None,
                                embedding_json=vector,
                                metadata_json=chunk.metadata,
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
                                    "system_id": campaign.system_id,
                                    "campaign_id": campaign_id,
                                    "module_id": module_id,
                                    "scene_id": scene_id,
                                }
                            )
                            vector_documents.append(chunk.content)
                            session.add(
                                VectorIndexJob(
                                    id=job_id,
                                    system_id=campaign.system_id,
                                    collection="modules",
                                    entity_type="module_chunk",
                                    entity_id=chunk_id,
                                    payload={
                                        "document": chunk.content,
                                        "metadata": vector_metadata[-1],
                                        "embedding_model": embedder.model_name,
                                    },
                                )
                            )
                    scene_count += 1
            if vector_store and vector_values:
                try:
                    vector_store.upsert(
                        "modules",
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
            return ModuleIngestResult(
                module_id,
                False,
                len(parsed),
                scene_count,
                chunk_count,
                embedding_count,
            )

    def ingest_path(
        self,
        *,
        campaign_id: str,
        path: str | Path,
        source_key: str | None = None,
        title: str | None = None,
        parser: MarkdownModuleParser | None = None,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
    ) -> ModuleIngestResult:
        source_path = Path(path).expanduser().resolve()
        document = converter_for(source_path).convert(source_path)
        return self.ingest(
            campaign_id=campaign_id,
            source_key=source_key or source_path.name,
            title=title or source_path.stem,
            content=document.content,
            metadata={
                "source_path": str(source_path),
                "media_type": document.media_type,
                "page_count": document.page_count,
                **document.metadata,
            },
            parser=parser,
            embedder=embedder,
            vector_store=vector_store,
            source_path=str(source_path),
            normalized_document=document,
        )

    def inspect_path(self, path: str | Path) -> dict[str, Any]:
        document = converter_for(path).convert(path)
        parsed = MarkdownModuleParser().parse(document.content)
        return {
            "source_path": document.source_path,
            "media_type": document.media_type,
            "checksum": document.checksum,
            "page_count": document.page_count,
            "warnings": list(document.warnings),
            "metadata": dict(document.metadata),
            "chapters": len(parsed),
            "scenes": sum(len(chapter.scenes) for chapter in parsed),
            "chunks": sum(
                len(scene.chunks)
                for chapter in parsed
                for scene in chapter.scenes
            ),
        }

    def list(self, campaign_id: str) -> list[dict[str, Any]]:
        with self.database.transaction() as session:
            rows = session.scalars(
                select(ModuleSource)
                .where(ModuleSource.campaign_id == campaign_id)
                .order_by(ModuleSource.title, ModuleSource.id)
            )
            return [
                {
                    "id": row.id,
                    "campaign_id": row.campaign_id,
                    "title": row.title,
                    "source_key": row.source_key,
                    "source_path": row.source_path,
                    "checksum": row.checksum,
                    "active": row.active,
                    "parser_profile": row.parser_profile,
                    "parser_version": row.parser_version,
                    "warnings": list(row.warnings),
                    "chapters": self._counts(session, row.id)[0],
                    "scenes": self._counts(session, row.id)[1],
                    "chunks": self._counts(session, row.id)[2],
                }
                for row in rows
            ]

    def expand(self, chunk_id: str) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = session.execute(
                select(ModuleChunk, ModuleScene, ModuleChapter, ModuleSource)
                .join(ModuleScene, ModuleScene.id == ModuleChunk.scene_id)
                .join(ModuleChapter, ModuleChapter.id == ModuleScene.chapter_id)
                .join(ModuleSource, ModuleSource.id == ModuleChunk.module_id)
                .where(ModuleChunk.id == chunk_id)
            ).one()
            return {
                "chunk_id": row.ModuleChunk.id,
                "content": row.ModuleChunk.content,
                "heading_path": list(row.ModuleChunk.heading_path),
                "chunk_type": row.ModuleChunk.chunk_type,
                "page_start": row.ModuleChunk.page_start,
                "page_end": row.ModuleChunk.page_end,
                "scene": {
                    "id": row.ModuleScene.id,
                    "title": row.ModuleScene.title,
                },
                "chapter": {
                    "id": row.ModuleChapter.id,
                    "title": row.ModuleChapter.title,
                },
                "module": {
                    "id": row.ModuleSource.id,
                    "title": row.ModuleSource.title,
                },
            }

    def read_scene(self, campaign_id: str, scene_id: str) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = session.execute(
                select(ModuleScene, ModuleChapter, ModuleSource)
                .join(ModuleChapter, ModuleChapter.id == ModuleScene.chapter_id)
                .join(ModuleSource, ModuleSource.id == ModuleScene.module_id)
                .where(
                    ModuleScene.id == scene_id,
                    ModuleSource.campaign_id == campaign_id,
                )
            ).one()
            return {
                "scene_id": row.ModuleScene.id,
                "title": row.ModuleScene.title,
                "content": row.ModuleScene.content,
                "page_start": row.ModuleScene.page_start,
                "page_end": row.ModuleScene.page_end,
                "chapter": row.ModuleChapter.title,
                "module": row.ModuleSource.title,
                "keywords": list(row.ModuleScene.keywords),
            }

    def scene_index(
        self,
        campaign_id: str,
        *,
        module_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a stable, portable scene index for agents and module generators."""
        with self.database.transaction() as session:
            statement = (
                select(ModuleScene, ModuleChapter, ModuleSource)
                .join(ModuleChapter, ModuleChapter.id == ModuleScene.chapter_id)
                .join(ModuleSource, ModuleSource.id == ModuleScene.module_id)
                .where(ModuleSource.campaign_id == campaign_id)
                .order_by(ModuleChapter.ordinal, ModuleScene.ordinal, ModuleScene.id)
            )
            if module_id:
                statement = statement.where(ModuleSource.id == module_id)
            return [
                {
                    "scene_id": row.ModuleScene.id,
                    "title": row.ModuleScene.title,
                    "chapter_id": row.ModuleChapter.id,
                    "chapter": row.ModuleChapter.title,
                    "module_id": row.ModuleSource.id,
                    "module": row.ModuleSource.title,
                    "page_start": row.ModuleScene.page_start,
                    "page_end": row.ModuleScene.page_end,
                    "keywords": list(row.ModuleScene.keywords),
                }
                for row in session.execute(statement)
            ]

    def set_active(self, campaign_id: str, module_id: str, *, active: bool) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = session.get(ModuleSource, module_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(module_id)
            row.active = active
            session.flush()
            return {"module_id": row.id, "active": row.active}

    def rename(self, campaign_id: str, module_id: str, title: str) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = session.get(ModuleSource, module_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(module_id)
            row.title = title
            session.flush()
            return {"module_id": row.id, "title": row.title}

    def delete(self, campaign_id: str, module_id: str) -> None:
        with self.database.transaction() as session:
            row = session.get(ModuleSource, module_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(module_id)
            session.delete(row)

    def search(
        self,
        *,
        campaign_id: str,
        query: str,
        top_k: int = 8,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
    ) -> list[SearchHit]:
        with self.database.transaction() as session:
            rows = session.execute(
                select(ModuleChunk, ModuleScene, ModuleSource)
                .join(ModuleScene, ModuleScene.id == ModuleChunk.scene_id)
                .join(ModuleSource, ModuleSource.id == ModuleChunk.module_id)
                .where(
                    ModuleSource.campaign_id == campaign_id,
                    ModuleSource.active.is_(True),
                )
            ).all()
        if not rows:
            return []

        exact = [
            row
            for row in rows
            if row.ModuleScene.title.casefold() == query.casefold()
            or row.ModuleSource.title.casefold() == query.casefold()
        ]
        lexical = sorted(
            rows,
            key=lambda row: -lexical_score(
                query,
                title=row.ModuleScene.title,
                content=row.ModuleChunk.content,
            ),
        )
        rankings = {
            "exact": [row.ModuleChunk.id for row in exact],
            "lexical": [row.ModuleChunk.id for row in lexical],
        }
        if embedder:
            query_vector = embedder.encode([query])[0]
            if vector_store and vector_store.enabled:
                rankings["dense"] = [
                    item_id
                    for item_id, _score in vector_store.query(
                        "modules",
                        query_embedding=query_vector,
                        limit=max(top_k * 4, 20),
                        where={"campaign_id": campaign_id},
                        profile=getattr(embedder, "profile", None),
                    )
                    if item_id in {row.ModuleChunk.id for row in rows}
                ]
            else:
                dense = sorted(
                    (
                        (
                            cosine_similarity(query_vector, row.ModuleChunk.embedding_json or []),
                            row,
                        )
                        for row in rows
                        if row.ModuleChunk.embedding_model == embedder.model_name
                    ),
                    key=lambda item: -item[0],
                )
                rankings["dense"] = [row.ModuleChunk.id for _, row in dense]

        by_id = {row.ModuleChunk.id: row for row in rows}
        hits = []
        for chunk_id, score, retrieval in reciprocal_rank_fusion(rankings)[:top_k]:
            row = by_id[chunk_id]
            hits.append(
                SearchHit(
                    id=chunk_id,
                    score=score,
                    title=row.ModuleScene.title,
                    content=row.ModuleChunk.content,
                    source_id=row.ModuleSource.id,
                    heading_path=tuple(row.ModuleChunk.heading_path),
                    retrieval=retrieval,
                    metadata={
                        "campaign_id": row.ModuleSource.campaign_id,
                        "module_title": row.ModuleSource.title,
                        "scene_id": row.ModuleScene.id,
                    },
                )
            )
        return hits

    def set_scene_progress(
        self,
        *,
        campaign_id: str,
        scene_id: str,
        status: str = "current",
        progress: int = 0,
        state: dict[str, Any] | None = None,
        current_room: str | None = None,
    ) -> dict[str, Any]:
        progress = max(0, min(100, progress))
        with self.database.transaction() as session:
            scene = session.get(ModuleScene, scene_id)
            if scene is None:
                raise LookupError(scene_id)
            source = session.get(ModuleSource, scene.module_id)
            if source is None or source.campaign_id != campaign_id:
                raise ValueError("scene does not belong to campaign")
            row = session.scalar(
                select(SceneProgress).where(
                    SceneProgress.campaign_id == campaign_id,
                    SceneProgress.scene_id == scene_id,
                )
            )
            if row is None:
                row = SceneProgress(
                    id=str(uuid.uuid4()),
                    campaign_id=campaign_id,
                    scene_id=scene_id,
                )
                session.add(row)
            row.status = status
            row.progress = progress
            row.current_room = current_room
            row.state_version = (row.state_version or 0) + 1
            row.state = state or {}
            session.flush()
            return {
                "id": row.id,
                "campaign_id": row.campaign_id,
                "scene_id": row.scene_id,
                "status": row.status,
                "progress": row.progress,
                "current_room": row.current_room,
                "state_version": row.state_version,
                "state": dict(row.state),
            }

    @staticmethod
    def _counts(session, module_id: str) -> tuple[int, int, int]:
        chapters = session.query(ModuleChapter).filter_by(module_id=module_id).count()
        scenes = session.query(ModuleScene).filter_by(module_id=module_id).count()
        chunks = session.query(ModuleChunk).filter_by(module_id=module_id).count()
        return chapters, scenes, chunks
