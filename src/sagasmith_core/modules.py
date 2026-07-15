"""Adventure-module parsing, ingestion, search, and scene progress."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select

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
    enrich_query,
    fts5_hits,
    reciprocal_rank_fusion,
    structured_score,
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


@dataclass(frozen=True)
class SceneBoundary:
    title: str
    start: int
    end: int
    metadata: dict[str, Any] = field(default_factory=dict)


class ModuleStructureProfile(Protocol):
    name: str
    version: str

    def classify_chunk(self, heading: str, text: str) -> str: ...

    def keywords(self, title: str, text: str) -> list[str]: ...

    def scene_boundaries(
        self,
        chapter_title: str,
        chapter_content: str,
    ) -> list[SceneBoundary]: ...


class GenericModuleProfile:
    name = "generic"
    version = "1"

    def classify_chunk(self, heading: str, text: str) -> str:
        lines = [line for line in text.splitlines() if line.strip()]
        if lines and all(line.lstrip().startswith("|") for line in lines):
            return "table"
        if text.lstrip().startswith(">"):
            return "read_aloud"
        if lines and sum(line.lstrip().startswith(("-", "*")) for line in lines) >= len(lines) / 2:
            return "list"
        if heading.casefold() in {"appendix", "附录", "reference", "参考"}:
            return "reference"
        return "narrative"

    def keywords(self, title: str, text: str) -> list[str]:
        values = re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}|[\u4e00-\u9fff]{2,8}", title)
        return list(dict.fromkeys(value.casefold() for value in values))[:20]

    def scene_boundaries(
        self,
        chapter_title: str,
        chapter_content: str,
    ) -> list[SceneBoundary]:
        matches = list(re.finditer(r"^(#{2,4})\s+(.+?)\s*$", chapter_content, re.MULTILINE))
        counts = {
            level: sum(len(match.group(1)) == level for match in matches) for level in (2, 3, 4)
        }
        if counts[2] and counts[3] >= counts[2] * 5:
            scene_level = 3
        elif counts[2]:
            scene_level = 2
        elif counts[3]:
            scene_level = 3
        else:
            scene_level = 4
        scene_headings = [match for match in matches if len(match.group(1)) == scene_level]
        if not scene_headings:
            return [SceneBoundary(chapter_title, 0, len(chapter_content))]
        return [
            SceneBoundary(
                heading.group(2).strip(),
                heading.start(),
                (
                    scene_headings[index + 1].start()
                    if index + 1 < len(scene_headings)
                    else len(chapter_content)
                ),
                {"scene_level": scene_level},
            )
            for index, heading in enumerate(scene_headings)
        ]


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
            (index, match) for index, match in enumerate(headings) if len(match.group(1)) == 1
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
        boundary_factory = getattr(self.profile, "scene_boundaries", None)
        ranges = (
            boundary_factory(title, chapter_content)
            if callable(boundary_factory)
            else GenericModuleProfile().scene_boundaries(title, chapter_content)
        )

        scenes: list[ParsedScene] = []
        for scene_ordinal, boundary in enumerate(ranges):
            scene_title = boundary.title
            start = boundary.start
            end = boundary.end
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
                        "end_line": chapter_content.count("\n", 0, start + chunk.end_offset) + 1,
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
                        **boundary.metadata,
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
        activate: bool = True,
        logical_source_key: str | None = None,
    ) -> ModuleIngestResult:
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        parsed = (parser or MarkdownModuleParser()).parse(content)
        logical_key = logical_source_key or source_key
        stored_source_key = source_key if activate else f"{logical_key}--staged-{checksum[:12]}"
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            existing = session.scalar(
                select(ModuleSource).where(
                    ModuleSource.campaign_id == campaign_id,
                    ModuleSource.source_key == stored_source_key,
                )
            )
            if existing and existing.checksum == checksum:
                counts = self._counts(session, existing.id)
                return ModuleIngestResult(existing.id, True, *counts, 0)
            if existing and activate:
                # Module text is external to save payloads, but scene progress
                # and historical snapshots hold foreign keys to its scene rows.
                # Retire the old source instead of deleting it: a restore must
                # always be able to resolve the exact scene it captured.
                existing.active = False
                existing.source_key = self._retired_source_key(
                    session, campaign_id, stored_source_key, existing.checksum
                )
                session.flush()

            module_id = str(uuid.uuid4())
            profile = getattr(parser, "profile", GenericModuleProfile())
            source_row = ModuleSource(
                id=module_id,
                system_id=campaign.system_id,
                campaign_id=campaign_id,
                source_key=stored_source_key,
                title=title,
                source_path=source_path,
                checksum=checksum,
                active=activate,
                parser_profile=getattr(profile, "name", "generic"),
                parser_version=getattr(profile, "version", "1"),
                warnings=list(normalized_document.warnings) if normalized_document else [],
                metadata_json={
                    **dict(metadata or {}),
                    "logical_source_key": logical_key,
                    "import_state": "active" if activate else "staged",
                },
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
                    scene_metadata = {
                        **dict(scene.metadata),
                        "stable_key": self._scene_stable_key(scene.heading_path, scene.title),
                        "content_checksum": hashlib.sha256(
                            scene.content.encode("utf-8")
                        ).hexdigest(),
                    }
                    session.add(
                        ModuleScene(
                            id=scene_id,
                            module_id=module_id,
                            chapter_id=chapter_id,
                            ordinal=scene.ordinal,
                            title=scene.title,
                            content=scene.content,
                            scene_type=scene_metadata.get("scene_type", "section"),
                            start_line=scene_metadata.get("start_line", 1),
                            end_line=scene_metadata.get("end_line", 1),
                            page_start=scene_metadata.get("page_start"),
                            page_end=scene_metadata.get("page_end"),
                            headings=scene_metadata.get(
                                "headings",
                                list(scene.heading_path),
                            ),
                            keywords=scene_metadata.get("keywords", []),
                            metadata_json=scene_metadata,
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
        activate: bool = True,
        logical_source_key: str | None = None,
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
            activate=activate,
            logical_source_key=logical_source_key,
        )

    def inspect_path(
        self,
        path: str | Path,
        *,
        parser: MarkdownModuleParser | None = None,
    ) -> dict[str, Any]:
        document = converter_for(path).convert(path)
        selected_parser = parser or MarkdownModuleParser()
        parsed = selected_parser.parse(document.content)
        return {
            "source_path": document.source_path,
            "media_type": document.media_type,
            "checksum": document.checksum,
            "page_count": document.page_count,
            "warnings": list(document.warnings),
            "metadata": dict(document.metadata),
            "parser_profile": getattr(selected_parser.profile, "name", "generic"),
            "parser_version": getattr(selected_parser.profile, "version", "1"),
            "chapters": len(parsed),
            "scenes": sum(len(chapter.scenes) for chapter in parsed),
            "chunks": sum(len(scene.chunks) for chapter in parsed for scene in chapter.scenes),
        }

    def preview_path(
        self,
        path: str | Path,
        *,
        parser: MarkdownModuleParser | None = None,
    ) -> dict[str, Any]:
        """Parse a module without persistence and expose stable scene/package evidence."""
        document = converter_for(path).convert(path)
        selected_parser = parser or MarkdownModuleParser()
        parsed = selected_parser.parse(document.content)
        scenes: list[dict[str, Any]] = []
        errors: list[str] = []
        keys: set[str] = set()
        for chapter in parsed:
            for scene in chapter.scenes:
                stable_key = self._scene_stable_key(scene.heading_path, scene.title)
                if stable_key in keys:
                    errors.append(f"duplicate stable scene key: {stable_key}")
                keys.add(stable_key)
                metadata = dict(scene.metadata)
                spatial = dict(metadata.get("spatial") or {})
                locations = list(spatial.get("locations") or [])
                location_keys = [str(item.get("key") or "") for item in locations]
                if any(not item for item in location_keys):
                    errors.append(f"scene {stable_key} has a spatial location without a key")
                if len(location_keys) != len(set(location_keys)):
                    errors.append(f"scene {stable_key} has duplicate spatial location keys")
                scenes.append(
                    {
                        "stable_key": stable_key,
                        "chapter": chapter.title,
                        "chapter_ordinal": chapter.ordinal,
                        "ordinal": scene.ordinal,
                        "title": scene.title,
                        "headings": list(scene.heading_path),
                        "scene_type": metadata.get("scene_type", "section"),
                        "visibility": metadata.get("visibility", "keeper"),
                        "keywords": list(metadata.get("keywords") or []),
                        "spatial": spatial,
                        "content_checksum": hashlib.sha256(
                            scene.content.encode("utf-8")
                        ).hexdigest(),
                    }
                )
        if not scenes:
            errors.append("module contains no scenes")
        return {
            "source_path": document.source_path,
            "media_type": document.media_type,
            "checksum": document.checksum,
            "page_count": document.page_count,
            "warnings": list(document.warnings),
            "metadata": dict(document.metadata),
            "parser_profile": getattr(selected_parser.profile, "name", "generic"),
            "parser_version": getattr(selected_parser.profile, "version", "1"),
            "scenes": scenes,
            "valid": not errors,
            "errors": errors,
        }

    def diff_preview(
        self,
        campaign_id: str,
        *,
        source_key: str,
        preview: dict[str, Any],
    ) -> dict[str, Any]:
        """Compare a prospective module package against its active logical revision."""
        with self.database.transaction() as session:
            sources = list(
                session.scalars(
                    select(ModuleSource)
                    .where(ModuleSource.campaign_id == campaign_id)
                    .where(ModuleSource.active.is_(True))
                )
            )
            current = next(
                (
                    row
                    for row in sources
                    if str(
                        dict(row.metadata_json or {}).get("logical_source_key") or row.source_key
                    )
                    == source_key
                ),
                None,
            )
            new_scenes = {str(item["stable_key"]): dict(item) for item in preview.get("scenes", [])}
            if current is None:
                return {
                    "source_key": source_key,
                    "current_module_id": None,
                    "added": sorted(new_scenes),
                    "removed": [],
                    "changed": [],
                    "unchanged": [],
                    "progress_impact": [],
                }
            rows = session.execute(
                select(ModuleScene, ModuleChapter)
                .join(ModuleChapter, ModuleChapter.id == ModuleScene.chapter_id)
                .where(ModuleScene.module_id == current.id)
            ).all()
            old_scenes: dict[str, tuple[ModuleScene, ModuleChapter]] = {}
            for scene, chapter in rows:
                metadata = dict(scene.metadata_json or {})
                stable_key = str(
                    metadata.get("stable_key")
                    or self._scene_stable_key((chapter.title, scene.title), scene.title)
                )
                old_scenes[stable_key] = (scene, chapter)
            added = sorted(set(new_scenes) - set(old_scenes))
            removed = sorted(set(old_scenes) - set(new_scenes))
            shared = sorted(set(old_scenes) & set(new_scenes))
            changed = [
                key
                for key in shared
                if str(dict(old_scenes[key][0].metadata_json or {}).get("content_checksum") or "")
                != str(new_scenes[key].get("content_checksum") or "")
            ]
            unchanged = sorted(set(shared) - set(changed))
            old_ids = {scene.id: key for key, (scene, _chapter) in old_scenes.items()}
            progress_rows = list(
                session.scalars(
                    select(SceneProgress).where(
                        SceneProgress.campaign_id == campaign_id,
                        SceneProgress.scene_id.in_(list(old_ids) or [""]),
                    )
                )
            )
            impact = [
                {
                    "scope_id": row.scope_id,
                    "scene_id": row.scene_id,
                    "stable_key": old_ids[row.scene_id],
                    "action": "remap" if old_ids[row.scene_id] in new_scenes else "needs_dm_review",
                    "target_stable_key": (
                        old_ids[row.scene_id] if old_ids[row.scene_id] in new_scenes else None
                    ),
                }
                for row in progress_rows
            ]
            return {
                "source_key": source_key,
                "current_module_id": current.id,
                "added": added,
                "removed": removed,
                "changed": changed,
                "unchanged": unchanged,
                "progress_impact": impact,
            }

    def list(self, campaign_id: str, *, include_retired: bool = False) -> list[dict[str, Any]]:
        with self.database.transaction() as session:
            statement = select(ModuleSource).where(ModuleSource.campaign_id == campaign_id)
            if not include_retired:
                statement = statement.where(ModuleSource.active.is_(True))
            rows = session.scalars(statement.order_by(ModuleSource.title, ModuleSource.id))
            return [
                {
                    "id": row.id,
                    "campaign_id": row.campaign_id,
                    "title": row.title,
                    "source_key": row.source_key,
                    "logical_source_key": str(
                        dict(row.metadata_json or {}).get("logical_source_key") or row.source_key
                    ),
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
                "campaign_id": row.ModuleSource.campaign_id,
                "content": row.ModuleChunk.content,
                "heading_path": list(row.ModuleChunk.heading_path),
                "chunk_type": row.ModuleChunk.chunk_type,
                "page_start": row.ModuleChunk.page_start,
                "page_end": row.ModuleChunk.page_end,
                "scene": {
                    "id": row.ModuleScene.id,
                    "title": row.ModuleScene.title,
                    "page_start": row.ModuleScene.page_start,
                    "page_end": row.ModuleScene.page_end,
                    **self._scene_structure(row.ModuleScene),
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
            metadata = dict(row.ModuleScene.metadata_json or {})
            return {
                "scene_id": row.ModuleScene.id,
                "stable_key": metadata.get("stable_key"),
                "title": row.ModuleScene.title,
                "content": row.ModuleScene.content,
                "page_start": row.ModuleScene.page_start,
                "page_end": row.ModuleScene.page_end,
                "chapter_id": row.ModuleChapter.id,
                "chapter": row.ModuleChapter.title,
                "chapter_ordinal": row.ModuleChapter.ordinal,
                "scene_ordinal": row.ModuleScene.ordinal,
                "module": row.ModuleSource.title,
                "module_id": row.ModuleSource.id,
                "start_line": row.ModuleScene.start_line,
                "end_line": row.ModuleScene.end_line,
                "keywords": list(row.ModuleScene.keywords),
                **self._scene_structure(row.ModuleScene),
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
                .where(ModuleSource.active.is_(True))
                .order_by(ModuleChapter.ordinal, ModuleScene.ordinal, ModuleScene.id)
            )
            if module_id:
                statement = statement.where(ModuleSource.id == module_id)
            return [
                {
                    "scene_id": row.ModuleScene.id,
                    "stable_key": dict(row.ModuleScene.metadata_json or {}).get("stable_key"),
                    "title": row.ModuleScene.title,
                    "chapter_id": row.ModuleChapter.id,
                    "chapter": row.ModuleChapter.title,
                    "chapter_ordinal": row.ModuleChapter.ordinal,
                    "scene_ordinal": row.ModuleScene.ordinal,
                    "module_id": row.ModuleSource.id,
                    "module": row.ModuleSource.title,
                    "page_start": row.ModuleScene.page_start,
                    "page_end": row.ModuleScene.page_end,
                    "start_line": row.ModuleScene.start_line,
                    "end_line": row.ModuleScene.end_line,
                    "keywords": list(row.ModuleScene.keywords),
                    **self._scene_structure(row.ModuleScene),
                }
                for row in session.execute(statement)
            ]

    def current_scene(
        self,
        campaign_id: str,
        *,
        scope_id: str = "party",
        fallback_to_party: bool = True,
    ) -> dict[str, Any] | None:
        with self.database.transaction() as session:
            row = None
            scopes = [scope_id]
            if fallback_to_party and scope_id != "party":
                scopes.append("party")
            for effective_scope in scopes:
                row = session.execute(
                    select(SceneProgress, ModuleScene, ModuleChapter, ModuleSource)
                    .join(ModuleScene, ModuleScene.id == SceneProgress.scene_id)
                    .join(ModuleChapter, ModuleChapter.id == ModuleScene.chapter_id)
                    .join(ModuleSource, ModuleSource.id == ModuleScene.module_id)
                    .where(
                        SceneProgress.campaign_id == campaign_id,
                        SceneProgress.scope_id == effective_scope,
                        SceneProgress.status == "current",
                    )
                    .order_by(SceneProgress.updated_at.desc(), SceneProgress.id.desc())
                ).first()
                if row is not None:
                    break
            if row is None:
                return None
            return {
                "campaign_id": campaign_id,
                "scope_id": row.SceneProgress.scope_id,
                "requested_scope_id": scope_id,
                "inherited_from_party": (
                    scope_id != "party" and row.SceneProgress.scope_id == "party"
                ),
                "scene_id": row.ModuleScene.id,
                "stable_key": dict(row.ModuleScene.metadata_json or {}).get("stable_key"),
                "title": row.ModuleScene.title,
                "content": row.ModuleScene.content,
                "chapter_id": row.ModuleChapter.id,
                "chapter": row.ModuleChapter.title,
                "chapter_ordinal": row.ModuleChapter.ordinal,
                "scene_ordinal": row.ModuleScene.ordinal,
                "module_id": row.ModuleSource.id,
                "module": row.ModuleSource.title,
                "page_start": row.ModuleScene.page_start,
                "page_end": row.ModuleScene.page_end,
                "start_line": row.ModuleScene.start_line,
                "end_line": row.ModuleScene.end_line,
                "keywords": list(row.ModuleScene.keywords),
                "progress": {
                    "status": row.SceneProgress.status,
                    "percent": row.SceneProgress.progress,
                    "current_room": row.SceneProgress.current_room,
                    "current_location_key": row.SceneProgress.current_location_key,
                    "state_version": row.SceneProgress.state_version,
                    "state": dict(row.SceneProgress.state),
                },
                **self._scene_structure(row.ModuleScene),
            }

    def scene_progress_index(
        self,
        campaign_id: str,
        *,
        scope_id: str = "party",
        module_id: str | None = None,
        fallback_to_party: bool = True,
    ) -> list[dict[str, Any]]:
        """Return ordered progress projected for one audience scope.

        A player/group scope overrides party progress scene by scene. Missing
        scoped rows may inherit party progress, matching :meth:`current_scene`.
        The response never merges mutable ``state`` dictionaries across scopes.
        """
        with self.database.transaction() as session:
            statement = (
                select(SceneProgress, ModuleScene, ModuleChapter, ModuleSource)
                .join(ModuleScene, ModuleScene.id == SceneProgress.scene_id)
                .join(ModuleChapter, ModuleChapter.id == ModuleScene.chapter_id)
                .join(ModuleSource, ModuleSource.id == ModuleScene.module_id)
                .where(ModuleSource.campaign_id == campaign_id)
                .where(ModuleSource.active.is_(True))
                .where(SceneProgress.scope_id.in_({scope_id, "party"}))
                .order_by(ModuleChapter.ordinal, ModuleScene.ordinal, ModuleScene.id)
            )
            if module_id:
                statement = statement.where(ModuleSource.id == module_id)
            by_scene: dict[str, dict[str, Any]] = {}
            for row in session.execute(statement):
                progress = row.SceneProgress
                inherited = progress.scope_id != scope_id
                if inherited and (scope_id == "party" or not fallback_to_party):
                    continue
                existing = by_scene.get(row.ModuleScene.id)
                if existing is not None and existing["scope_id"] == scope_id:
                    continue
                by_scene[row.ModuleScene.id] = {
                    "id": progress.id,
                    "campaign_id": campaign_id,
                    "scene_id": row.ModuleScene.id,
                    "stable_key": dict(row.ModuleScene.metadata_json or {}).get("stable_key"),
                    "module_id": row.ModuleSource.id,
                    "chapter_id": row.ModuleChapter.id,
                    "chapter_ordinal": row.ModuleChapter.ordinal,
                    "scene_ordinal": row.ModuleScene.ordinal,
                    "scope_id": progress.scope_id,
                    "requested_scope_id": scope_id,
                    "inherited_from_party": inherited,
                    "status": progress.status,
                    "percent": progress.progress,
                    "current_room": progress.current_room,
                    "current_location_key": progress.current_location_key,
                    "state_version": progress.state_version,
                    "state": dict(progress.state),
                }
            return sorted(
                by_scene.values(),
                key=lambda item: (
                    item["chapter_ordinal"],
                    item["scene_ordinal"],
                    item["scene_id"],
                ),
            )

    def set_active(self, campaign_id: str, module_id: str, *, active: bool) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = session.get(ModuleSource, module_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(module_id)
            row.active = active
            session.flush()
            return {"module_id": row.id, "active": row.active}

    def activate_candidate(self, campaign_id: str, module_id: str) -> dict[str, Any]:
        """Atomically make one staged revision current for its logical module key."""
        with self.database.transaction() as session:
            row = session.get(ModuleSource, module_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(module_id)
            logical_key = str(
                dict(row.metadata_json or {}).get("logical_source_key") or row.source_key
            )
            replaced: list[str] = []
            for candidate in session.scalars(
                select(ModuleSource).where(ModuleSource.campaign_id == campaign_id)
            ):
                candidate_key = str(
                    dict(candidate.metadata_json or {}).get("logical_source_key")
                    or candidate.source_key
                )
                if candidate.id != row.id and candidate.active and candidate_key == logical_key:
                    candidate.active = False
                    replaced.append(candidate.id)
            row.active = True
            row.metadata_json = {**dict(row.metadata_json or {}), "import_state": "active"}
            session.flush()
            return {"module_id": row.id, "active": True, "replaced_module_ids": replaced}

    @staticmethod
    def _scene_stable_key(heading_path: Sequence[str], title: str) -> str:
        source = "/".join(str(item).strip() for item in heading_path if str(item).strip())
        source = source or title
        normalized = re.sub(r"[^a-z0-9]+", "-", source.casefold()).strip("-")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        return normalized[:120] or f"scene-{digest}"

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
        query_hints: dict[str, Sequence[str]] | None = None,
    ) -> list[SearchHit]:
        enriched = enrich_query(query, extra_terms=query_hints)
        with self.database.transaction() as session:
            rows = session.execute(
                select(ModuleChunk, ModuleScene, ModuleChapter, ModuleSource)
                .join(ModuleScene, ModuleScene.id == ModuleChunk.scene_id)
                .join(ModuleChapter, ModuleChapter.id == ModuleScene.chapter_id)
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
            or row.ModuleChapter.title.casefold() == query.casefold()
            or row.ModuleSource.title.casefold() == query.casefold()
        ]
        exact_ids = {row.ModuleChunk.id for row in exact}

        # FTS5 lexical channel — indexed BM25 on SQLite, zero deps
        fts_ids: list[str] = []
        with self.database.transaction() as session:
            fts_ids = fts5_hits(
                session,
                "module_fts",
                enriched,
                limit=max(top_k * 4, 20),
                weights=(
                    0.0,  # chunk_id UNINDEXED
                    8.0,  # module_title
                    6.0,  # chapter_title
                    4.0,  # scene_title
                    3.0,  # headings
                    2.5,  # keywords
                    2.0,  # tags
                    2.0,  # scene_type
                    1.5,  # chunk_type
                    1.0,  # content
                ),
            )
            if fts_ids:
                fts_filtered = [
                    chunk_id
                    for chunk_id in fts_ids
                    if chunk_id in {row.ModuleChunk.id for row in rows}
                ]
                fts_ids = fts_filtered

        if fts_ids:
            lexical = fts_ids
        else:
            # Fallback: Python-side structured_score when FTS5 unavailable
            lexical = [
                row.ModuleChunk.id
                for row in sorted(
                    rows,
                    key=lambda row: (
                        -structured_score(
                            enriched,
                            module_title=row.ModuleSource.title,
                            chapter_title=row.ModuleChapter.title,
                            scene_title=row.ModuleScene.title,
                            heading_paths=" ".join(row.ModuleScene.headings or []),
                            keywords=" ".join(row.ModuleScene.keywords or []),
                            tags=" ".join(row.ModuleScene.metadata_json.get("tags", [])),
                            scene_type=row.ModuleScene.scene_type,
                            chunk_type=row.ModuleChunk.chunk_type,
                            content=row.ModuleChunk.content,
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
                        "scene_type": row.ModuleScene.scene_type,
                        "visibility": row.ModuleScene.metadata_json.get(
                            "visibility",
                            "keeper",
                        ),
                        "page_start": row.ModuleChunk.page_start,
                        "page_end": row.ModuleChunk.page_end,
                        "chunk_type": row.ModuleChunk.chunk_type,
                        "tags": row.ModuleScene.metadata_json.get("tags", []),
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
        current_location_key: str | None = None,
        scope_id: str = "party",
        expected_state_version: int | None = None,
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
                    SceneProgress.scope_id == scope_id,
                    SceneProgress.scene_id == scene_id,
                )
            )
            if row is None:
                if expected_state_version not in {None, 0}:
                    raise ValueError(
                        f"scene progress conflict: expected {expected_state_version}, found 0"
                    )
                row = SceneProgress(
                    id=str(uuid.uuid4()),
                    campaign_id=campaign_id,
                    scene_id=scene_id,
                    scope_id=scope_id,
                )
                session.add(row)
            elif expected_state_version is not None and row.state_version != expected_state_version:
                raise ValueError(
                    f"scene progress conflict: expected {expected_state_version}, "
                    f"found {row.state_version}"
                )
            if status == "current":
                for other in session.scalars(
                    select(SceneProgress).where(
                        SceneProgress.campaign_id == campaign_id,
                        SceneProgress.scope_id == scope_id,
                        SceneProgress.scene_id != scene_id,
                        SceneProgress.status == "current",
                    )
                ):
                    other.status = "previous"
            row.status = status
            row.progress = progress
            if current_room is not None:
                row.current_room = current_room
            if current_location_key is not None:
                locations = {
                    str(item.get("key"))
                    for item in dict(scene.metadata_json or {})
                    .get("spatial", {})
                    .get("locations", [])
                    if isinstance(item, dict) and item.get("key")
                }
                if locations and current_location_key not in locations:
                    raise ValueError("current_location_key is not a location in this scene")
                row.current_location_key = current_location_key
            row.state_version = (row.state_version or 0) + 1
            if state is not None:
                row.state = state
            session.flush()
            return {
                "id": row.id,
                "campaign_id": row.campaign_id,
                "scene_id": row.scene_id,
                "scope_id": row.scope_id,
                "status": row.status,
                "progress": row.progress,
                "current_room": row.current_room,
                "current_location_key": row.current_location_key,
                "state_version": row.state_version,
                "state": dict(row.state),
            }

    @staticmethod
    def _scene_structure(scene: ModuleScene) -> dict[str, Any]:
        """Build a scene-structure dict from DB columns + profile-populated metadata.

        Column-backed fields (always populated regardless of profile):
          scene_type, headings

        Fields set by any profile that implements ``scene_boundaries()``:
          scene_level, line_count, subsections, tags

        Fields set only by certain system profiles — **not** guaranteed for every
        system. Consumers must treat missing/empty values as "not provided by the
        profile that parsed this module", not "zero of that thing exists":

          - ``visibility`` — defaulted to ``"keeper"`` if the profile omits it
          - ``clues``, ``checks``       — CoC profile populates these
          - ``sanity``                  — CoC profile only
          - ``transitions``, ``node_id`` — CoC ``solo_scenario`` parsing only
        """
        metadata = dict(scene.metadata_json or {})
        return {
            "scene_type": scene.scene_type,
            "visibility": metadata.get("visibility", "keeper"),
            "scene_level": metadata.get("scene_level"),
            "line_count": metadata.get("line_count"),
            "headings": list(scene.headings),
            "subsections": list(metadata.get("subsections", [])),
            "tags": list(metadata.get("tags", [])),
            "clues": list(metadata.get("clues", [])),
            "checks": list(metadata.get("checks", [])),
            "sanity": list(metadata.get("sanity", [])),
            "transitions": list(metadata.get("transitions", [])),
            "node_id": metadata.get("node_id"),
            "spatial": dict(metadata.get("spatial") or {}),
        }

    @staticmethod
    def _retired_source_key(session: Any, campaign_id: str, source_key: str, checksum: str) -> str:
        """Return a unique, human-auditable key for an immutable retired revision."""
        # ModuleSource.source_key is capped at 200 characters. Reserve room for
        # the checksum and a collision suffix when a very long filename is
        # revised multiple times.
        stem = f"{source_key[:180]}@{checksum[:12]}"
        candidate = stem
        suffix = 2
        while session.scalar(
            select(ModuleSource.id).where(
                ModuleSource.campaign_id == campaign_id,
                ModuleSource.source_key == candidate,
            )
        ):
            candidate = f"{stem[: 200 - len(str(suffix)) - 1]}-{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _counts(session, module_id: str) -> tuple[int, int, int]:
        chapters = session.query(ModuleChapter).filter_by(module_id=module_id).count()
        scenes = session.query(ModuleScene).filter_by(module_id=module_id).count()
        chunks = session.query(ModuleChunk).filter_by(module_id=module_id).count()
        return chapters, scenes, chunks
