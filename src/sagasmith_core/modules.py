"""Adventure-module parsing, ingestion, search, and scene progress."""

from __future__ import annotations

import hashlib
import json
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
    ModuleContentReview,
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

    def document_metadata(self, content: str) -> dict[str, Any]: ...


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

    def document_metadata(self, content: str) -> dict[str, Any]:
        """Return profile-owned document metadata without interpreting generic prose."""
        return {}


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
        structural_starts = [
            self._structural_start(content, heading) for _, heading in chapter_starts
        ]
        first_chapter_start = structural_starts[0]
        preamble = content[:first_chapter_start]
        if chapter_starts[0][1].group(1) == "#" and strip_page_markers(preamble).strip():
            parsed.append(self._chapter(0, "Front Matter", preamble, 0, first_chapter_start))
        for ordinal, (_heading_index, heading) in enumerate(chapter_starts):
            start = structural_starts[ordinal]
            end = (
                structural_starts[ordinal + 1]
                if ordinal + 1 < len(chapter_starts)
                else len(content)
            )
            title = heading.group(2).strip()
            parsed.append(self._chapter(len(parsed), title, content[start:end], start, end))
        return parsed

    def document_metadata(self, content: str) -> dict[str, Any]:
        factory = getattr(self.profile, "document_metadata", None)
        return dict(factory(content)) if callable(factory) else {}

    @staticmethod
    def _structural_start(content: str, heading: re.Match[str]) -> int:
        """Keep a page marker immediately preceding a chapter with that chapter."""
        prefix = content[: heading.start()]
        markers = list(re.finditer(r"^<!-- page: \d+ -->\s*$", prefix, re.MULTILINE))
        if not markers:
            return heading.start()
        marker = markers[-1]
        return marker.start() if not prefix[marker.end() :].strip() else heading.start()

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
        selected_parser = parser or MarkdownModuleParser()
        parsed = selected_parser.parse(content)
        profile_metadata = selected_parser.document_metadata(content)
        manifest_errors = list(profile_metadata.get("runtime_manifest_errors") or [])
        if manifest_errors:
            raise ValueError("invalid module runtime manifest: " + "; ".join(manifest_errors))
        profile = getattr(selected_parser, "profile", GenericModuleProfile())
        parser_profile = getattr(profile, "name", "generic")
        parser_version = getattr(profile, "version", "1")
        logical_key = logical_source_key or source_key
        stored_source_key = (
            source_key
            if activate
            else (
                f"{logical_key}--staged-{checksum[:12]}-"
                f"{parser_profile}-{parser_version}"
            )
        )
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
            if (
                existing
                and existing.checksum == checksum
                and existing.parser_profile == parser_profile
                and existing.parser_version == parser_version
            ):
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
            source_row = ModuleSource(
                id=module_id,
                system_id=campaign.system_id,
                campaign_id=campaign_id,
                source_key=stored_source_key,
                title=title,
                source_path=source_path,
                checksum=checksum,
                active=activate,
                parser_profile=parser_profile,
                parser_version=parser_version,
                warnings=list(normalized_document.warnings) if normalized_document else [],
                metadata_json={
                    **dict(metadata or {}),
                    **profile_metadata,
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
            stable_keys = self._scene_stable_keys(parsed)
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
                        "stable_key": stable_keys[(chapter.ordinal, scene.ordinal)],
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
        profile_metadata = selected_parser.document_metadata(document.content)
        return {
            "source_path": document.source_path,
            "media_type": document.media_type,
            "checksum": document.checksum,
            "page_count": document.page_count,
            "warnings": list(document.warnings),
            "metadata": dict(document.metadata),
            "profile_metadata": profile_metadata,
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
        profile_metadata = selected_parser.document_metadata(document.content)
        scenes: list[dict[str, Any]] = []
        errors: list[str] = list(profile_metadata.get("runtime_manifest_errors") or [])
        keys: set[str] = set()
        stable_keys = self._scene_stable_keys(parsed)
        for chapter in parsed:
            for scene in chapter.scenes:
                stable_key = stable_keys[(chapter.ordinal, scene.ordinal)]
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
                page_start = metadata.get("page_start")
                page_end = metadata.get("page_end")
                if document.media_type == "application/pdf" and document.page_count is not None:
                    if page_start is None or page_end is None:
                        errors.append(f"scene {stable_key} has no PDF page range")
                    elif not (1 <= int(page_start) <= int(page_end) <= document.page_count):
                        errors.append(
                            f"scene {stable_key} has invalid PDF page range "
                            f"{page_start}-{page_end}"
                        )
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
                        "page_start": page_start,
                        "page_end": page_end,
                        "start_line": metadata.get("start_line"),
                        "end_line": metadata.get("end_line"),
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
            "profile_metadata": profile_metadata,
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
                    "runtime_manifest": dict(row.metadata_json or {}).get(
                        "runtime_manifest"
                    ),
                    "chapters": self._counts(session, row.id)[0],
                    "scenes": self._counts(session, row.id)[1],
                    "chunks": self._counts(session, row.id)[2],
                }
                for row in rows
            ]

    def list_assets(self, campaign_id: str, module_id: str) -> list[dict[str, Any]]:
        """List source and derived assets belonging to one campaign module."""
        with self.database.transaction() as session:
            source = session.get(ModuleSource, module_id)
            if source is None or source.campaign_id != campaign_id:
                raise LookupError(module_id)
            rows = session.scalars(
                select(ModuleAsset)
                .where(ModuleAsset.module_id == module_id)
                .order_by(ModuleAsset.created_at, ModuleAsset.id)
            )
            return [self._asset_view(row) for row in rows]

    def get_asset(self, campaign_id: str, asset_id: str) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = session.get(ModuleAsset, asset_id)
            if row is None:
                raise LookupError(asset_id)
            source = session.get(ModuleSource, row.module_id)
            if source is None or source.campaign_id != campaign_id:
                raise LookupError(asset_id)
            return self._asset_view(row)

    def register_asset(
        self,
        *,
        campaign_id: str,
        module_id: str,
        source_path: str,
        media_type: str,
        checksum: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Idempotently register a managed derived module asset."""
        resolved = str(Path(source_path).expanduser().resolve())
        with self.database.transaction() as session:
            source = session.get(ModuleSource, module_id)
            if source is None or source.campaign_id != campaign_id:
                raise LookupError(module_id)
            row = session.scalar(
                select(ModuleAsset).where(
                    ModuleAsset.module_id == module_id,
                    ModuleAsset.source_path == resolved,
                )
            )
            if row is None:
                row = ModuleAsset(
                    id=str(uuid.uuid4()),
                    module_id=module_id,
                    source_path=resolved,
                    media_type=media_type,
                    checksum=checksum,
                    normalized_content=None,
                    metadata_json=dict(metadata or {}),
                )
                session.add(row)
            elif row.checksum != checksum:
                raise ValueError("managed module asset path has different content")
            else:
                row.media_type = media_type
                row.metadata_json = {**dict(row.metadata_json or {}), **dict(metadata or {})}
            session.flush()
            return self._asset_view(row)

    def review_content(
        self,
        *,
        campaign_id: str,
        module_id: str,
        scene_id: str,
        content_key: str,
        content_kind: str,
        normalized_content: str,
        source_asset_id: str,
        page_number: int,
        reviewer: str,
        observation: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an immutable human/agent-reviewed transcription with page evidence."""
        key = str(content_key).strip()
        kind = str(content_kind).strip()
        content = str(normalized_content).strip()
        reviewer_value = str(reviewer).strip()
        observation_value = " ".join(str(observation).split()).strip()
        if not key or len(key) > 200:
            raise ValueError("content_key must contain 1 to 200 characters")
        if not kind or len(kind) > 100:
            raise ValueError("content_kind must contain 1 to 100 characters")
        if not content:
            raise ValueError("normalized_content is required")
        if len(content) > 200_000:
            raise ValueError("normalized_content exceeds 200000 characters")
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            raise ValueError("page_number must be a 1-based integer")
        if not reviewer_value:
            raise ValueError("reviewer is required")
        if not observation_value or len(observation_value) > 500:
            raise ValueError("observation must contain 1 to 500 characters")
        metadata_value = dict(metadata or {})
        checksum = hashlib.sha256(
            json.dumps(
                {
                    "content_key": key,
                    "content_kind": kind,
                    "normalized_content": content,
                    "metadata": metadata_value,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        with self.database.transaction() as session:
            source = session.get(ModuleSource, module_id)
            if source is None or source.campaign_id != campaign_id:
                raise LookupError(module_id)
            scene = session.get(ModuleScene, scene_id)
            if scene is None or scene.module_id != module_id:
                raise ValueError("content review scene must belong to the module")
            asset = session.get(ModuleAsset, source_asset_id)
            if asset is None or asset.module_id != module_id:
                raise ValueError("content review asset must belong to the module")
            media_type = str(asset.media_type or "").casefold()
            if media_type != "application/pdf" and not media_type.startswith("image/"):
                raise ValueError("content review requires a PDF or rendered image asset")
            asset_metadata = dict(asset.metadata_json or {})
            if media_type == "application/pdf":
                page_count = int(asset_metadata.get("page_count") or 0)
                if page_count and page_number > page_count:
                    raise ValueError(f"content review page exceeds PDF page count {page_count}")
            else:
                source_page = int(asset_metadata.get("source_page") or 0)
                if source_page and source_page != page_number:
                    raise ValueError("content review page must match rendered asset source_page")

            existing = session.scalar(
                select(ModuleContentReview).where(
                    ModuleContentReview.module_id == module_id,
                    ModuleContentReview.scene_id == scene_id,
                    ModuleContentReview.content_key == key,
                    ModuleContentReview.checksum == checksum,
                )
            )
            if existing is not None:
                return self._content_review_view(existing)
            row = ModuleContentReview(
                id=str(uuid.uuid4()),
                module_id=module_id,
                scene_id=scene_id,
                content_key=key,
                content_kind=kind,
                normalized_content=content,
                checksum=checksum,
                evidence_json={
                    "asset_id": asset.id,
                    "asset_checksum": asset.checksum,
                    "page": page_number,
                    "reviewer": reviewer_value,
                    "observation": observation_value,
                    "confidence": "reviewed_image",
                },
                metadata_json=metadata_value,
            )
            session.add(row)
            session.flush()
            return self._content_review_view(row)

    def list_content_reviews(
        self,
        campaign_id: str,
        module_id: str,
        *,
        content_kind: str | None = None,
        content_key: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.database.transaction() as session:
            source = session.get(ModuleSource, module_id)
            if source is None or source.campaign_id != campaign_id:
                raise LookupError(module_id)
            query = select(ModuleContentReview).where(ModuleContentReview.module_id == module_id)
            if content_kind is not None:
                query = query.where(ModuleContentReview.content_kind == content_kind)
            if content_key is not None:
                query = query.where(ModuleContentReview.content_key == content_key)
            rows = session.scalars(
                query.order_by(ModuleContentReview.created_at, ModuleContentReview.id)
            )
            return [self._content_review_view(row) for row in rows]

    def get_content_review(self, campaign_id: str, review_id: str) -> dict[str, Any]:
        with self.database.transaction() as session:
            row = session.get(ModuleContentReview, review_id)
            if row is None:
                raise LookupError(review_id)
            source = session.get(ModuleSource, row.module_id)
            if source is None or source.campaign_id != campaign_id:
                raise LookupError(review_id)
            return self._content_review_view(row)

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

    def read_scene(
        self,
        campaign_id: str,
        scene_id: str,
        *,
        scope_id: str | None = None,
        fallback_to_party: bool = True,
    ) -> dict[str, Any]:
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
            progress_state: dict[str, Any] | None = None
            if scope_id is not None:
                scopes = [scope_id]
                if fallback_to_party and scope_id != "party":
                    scopes.append("party")
                for candidate_scope in scopes:
                    progress = session.scalar(
                        select(SceneProgress)
                        .where(
                            SceneProgress.campaign_id == campaign_id,
                            SceneProgress.scene_id == scene_id,
                            SceneProgress.scope_id == candidate_scope,
                        )
                        .order_by(SceneProgress.updated_at.desc(), SceneProgress.id.desc())
                    )
                    if progress is not None:
                        progress_state = dict(progress.state or {})
                        break
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
                **self._scene_structure(row.ModuleScene, progress_state=progress_state),
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
                **self._scene_structure(
                    row.ModuleScene,
                    progress_state=dict(row.SceneProgress.state or {}),
                ),
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
    def _scene_stable_keys(parsed: Sequence[ParsedChapter]) -> dict[tuple[int, int], str]:
        """Build deterministic keys and disambiguate repeated semantic headings."""
        result: dict[tuple[int, int], str] = {}
        occurrences: dict[str, int] = {}
        for chapter in parsed:
            for scene in chapter.scenes:
                base = ModuleService._scene_stable_key(scene.heading_path, scene.title)
                occurrences[base] = occurrences.get(base, 0) + 1
                occurrence = occurrences[base]
                result[(chapter.ordinal, scene.ordinal)] = (
                    base if occurrence == 1 else f"{base}--{occurrence}"
                )
        return result

    @staticmethod
    def _scene_stable_key(heading_path: Sequence[str], title: str) -> str:
        source = "/".join(str(item).strip() for item in heading_path if str(item).strip())
        source = source or title
        normalized = re.sub(r"[^\w]+", "-", source.casefold()).strip("-").replace("_", "-")
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
            or any(
                str(heading).casefold() == query.casefold()
                for heading in row.ModuleChunk.heading_path
            )
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
                            heading_paths=" ".join(row.ModuleChunk.heading_path or []),
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
        status: str | None = None,
        progress: int | None = None,
        state: dict[str, Any] | None = None,
        current_room: str | None = None,
        current_location_key: str | None = None,
        scope_id: str = "party",
        expected_state_version: int | None = None,
        spatial_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state is not None and spatial_review is not None:
            raise ValueError("state and spatial_review cannot be changed in the same request")
        if progress is not None:
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
            effective_status = status or row.status or "current"
            if effective_status == "current":
                for other in session.scalars(
                    select(SceneProgress).where(
                        SceneProgress.campaign_id == campaign_id,
                        SceneProgress.scope_id == scope_id,
                        SceneProgress.scene_id != scene_id,
                        SceneProgress.status == "current",
                    )
                ):
                    other.status = "previous"
            if status is not None:
                row.status = status
            if progress is not None:
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
                    matching_scenes = []
                    for candidate in session.scalars(
                        select(ModuleScene).where(
                            ModuleScene.module_id == scene.module_id,
                            ModuleScene.id != scene.id,
                        )
                    ):
                        candidate_locations = {
                            str(item.get("key"))
                            for item in dict(candidate.metadata_json or {})
                            .get("spatial", {})
                            .get("locations", [])
                            if isinstance(item, dict) and item.get("key")
                        }
                        if current_location_key in candidate_locations:
                            matching_scenes.append(candidate.id)
                    if len(matching_scenes) != 1:
                        raise ValueError(
                            "current_location_key must identify one location in the "
                            "current scene or exactly one scene in the same module"
                        )
                row.current_location_key = current_location_key
            row.state_version = (row.state_version or 0) + 1
            if state is not None:
                row.state = state
            elif spatial_review is not None:
                row.state = self._apply_spatial_review(
                    session,
                    scene=scene,
                    campaign_id=campaign_id,
                    state=dict(row.state or {}),
                    review=spatial_review,
                )
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
    def _apply_spatial_review(
        session: Any,
        *,
        scene: ModuleScene,
        campaign_id: str,
        state: dict[str, Any],
        review: dict[str, Any],
    ) -> dict[str, Any]:
        allowed_review_fields = {
            "schema_version",
            "mode",
            "source_asset_id",
            "page_number",
            "connections",
            "reviewer",
            "branch_id",
            "note",
        }
        unknown = set(review) - allowed_review_fields
        if unknown:
            raise ValueError(f"unsupported spatial_review fields: {sorted(unknown)}")
        if review.get("schema_version", 1) != 1:
            raise ValueError("spatial_review schema_version must be 1")
        mode = str(review.get("mode") or "merge")
        if mode not in {"merge", "replace"}:
            raise ValueError("spatial_review mode must be merge or replace")
        asset_id = str(review.get("source_asset_id") or "").strip()
        if not asset_id:
            raise ValueError("spatial_review source_asset_id is required")
        asset = session.get(ModuleAsset, asset_id)
        if asset is None or asset.module_id != scene.module_id:
            raise ValueError("spatial_review asset must belong to the scene module")
        source = session.get(ModuleSource, scene.module_id)
        if source is None or source.campaign_id != campaign_id:
            raise ValueError("scene does not belong to campaign")
        if asset.media_type not in {"application/pdf", "image/png", "image/jpeg"}:
            raise ValueError("spatial_review requires a PDF or rendered image asset")
        page_number = review.get("page_number")
        if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
            raise ValueError("spatial_review page_number must be a 1-based integer")
        asset_metadata = dict(asset.metadata_json or {})
        if asset.media_type == "application/pdf":
            page_count = int(asset_metadata.get("page_count") or 0)
            if page_count and page_number > page_count:
                raise ValueError(f"spatial_review page exceeds PDF page count {page_count}")
        else:
            source_page = int(asset_metadata.get("source_page") or 0)
            if source_page and page_number != source_page:
                raise ValueError("spatial_review page must match the rendered asset source_page")

        location_counts: dict[str, int] = {}
        for candidate in session.scalars(
            select(ModuleScene).where(ModuleScene.module_id == scene.module_id)
        ):
            for location in (
                dict(candidate.metadata_json or {}).get("spatial", {}).get("locations", [])
            ):
                if not isinstance(location, dict) or not location.get("key"):
                    continue
                key = str(location["key"])
                location_counts[key] = location_counts.get(key, 0) + 1

        raw_connections = review.get("connections")
        if not isinstance(raw_connections, list) or not raw_connections:
            raise ValueError("spatial_review connections must be a non-empty list")
        if len(raw_connections) > 500:
            raise ValueError("spatial_review cannot change more than 500 connections at once")
        reviewer = str(review.get("reviewer") or "").strip()
        if not reviewer:
            raise ValueError("spatial_review reviewer is required")
        branch_id = str(review.get("branch_id") or "").strip()
        if not branch_id:
            raise ValueError("spatial_review branch_id is required")
        kinds = {"passage", "door", "secret_door", "stairs", "portal", "other"}
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_connections):
            if not isinstance(raw, dict):
                raise ValueError("each reviewed connection must be an object")
            unknown_connection = set(raw) - {
                "from",
                "to",
                "bidirectional",
                "kind",
                "observation",
            }
            if unknown_connection:
                raise ValueError(
                    f"unsupported reviewed connection fields at index {index}: "
                    f"{sorted(unknown_connection)}"
                )
            source_key = str(raw.get("from") or "").strip()
            target_key = str(raw.get("to") or "").strip()
            if not source_key or not target_key or source_key == target_key:
                raise ValueError("reviewed connection endpoints must be distinct location keys")
            for key in (source_key, target_key):
                if location_counts.get(key) != 1:
                    raise ValueError(
                        f"reviewed connection endpoint must identify exactly one module "
                        f"location: {key}"
                    )
            bidirectional = raw.get("bidirectional", True)
            if not isinstance(bidirectional, bool):
                raise ValueError("reviewed connection bidirectional must be boolean")
            kind = str(raw.get("kind") or "passage")
            if kind not in kinds:
                raise ValueError(f"unsupported reviewed connection kind: {kind}")
            observation = str(raw.get("observation") or "").strip()
            if not observation or len(observation) > 500:
                raise ValueError("reviewed connection observation must contain 1-500 characters")
            normalized.append(
                {
                    "from": source_key,
                    "to": target_key,
                    "bidirectional": bidirectional,
                    "kind": kind,
                    "confidence": "reviewed_image",
                    "evidence": {
                        "asset_id": asset.id,
                        "asset_checksum": asset.checksum,
                        "page": page_number,
                        "observation": observation,
                        "reviewer": reviewer,
                        "branch_id": branch_id,
                    },
                }
            )

        previous = dict(state.get("spatial_review") or {})
        existing = [] if mode == "replace" else list(previous.get("connections") or [])
        by_key: dict[tuple[str, str, bool], dict[str, Any]] = {}
        for connection in [*existing, *normalized]:
            if not isinstance(connection, dict):
                continue
            source_key = str(connection.get("from") or "")
            target_key = str(connection.get("to") or "")
            bidirectional = bool(connection.get("bidirectional", True))
            if bidirectional:
                source_key, target_key = sorted((source_key, target_key))
            by_key[(source_key, target_key, bidirectional)] = connection
        state["spatial_review"] = {
            "schema_version": 1,
            "connections": list(by_key.values()),
            "last_evidence": {
                "asset_id": asset.id,
                "page": page_number,
                "reviewer": reviewer,
                "branch_id": branch_id,
                "note": str(review.get("note") or "").strip(),
            },
        }
        return state

    @staticmethod
    def _scene_structure(
        scene: ModuleScene,
        *,
        progress_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
        spatial = dict(metadata.get("spatial") or {})
        reviewed = dict((progress_state or {}).get("spatial_review") or {})
        reviewed_connections = list(reviewed.get("connections") or [])
        if reviewed_connections:
            base_connections = list(spatial.get("connections") or [])
            by_key: dict[tuple[str, str, bool], dict[str, Any]] = {}
            for connection in [*base_connections, *reviewed_connections]:
                if not isinstance(connection, dict):
                    continue
                source_key = str(connection.get("from") or "")
                target_key = str(connection.get("to") or "")
                bidirectional = bool(connection.get("bidirectional", True))
                if bidirectional:
                    source_key, target_key = sorted((source_key, target_key))
                by_key[(source_key, target_key, bidirectional)] = connection
            spatial["connections"] = list(by_key.values())
            spatial["review"] = {
                "schema_version": reviewed.get("schema_version", 1),
                "connection_count": len(reviewed_connections),
                "last_evidence": dict(reviewed.get("last_evidence") or {}),
            }
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
            "spatial": spatial,
        }

    @staticmethod
    def _asset_view(asset: ModuleAsset) -> dict[str, Any]:
        return {
            "id": asset.id,
            "module_id": asset.module_id,
            "source_path": asset.source_path,
            "media_type": asset.media_type,
            "checksum": asset.checksum,
            "metadata": dict(asset.metadata_json or {}),
        }

    @staticmethod
    def _content_review_view(row: ModuleContentReview) -> dict[str, Any]:
        return {
            "id": row.id,
            "module_id": row.module_id,
            "scene_id": row.scene_id,
            "content_key": row.content_key,
            "content_kind": row.content_kind,
            "normalized_content": row.normalized_content,
            "checksum": row.checksum,
            "evidence": dict(row.evidence_json or {}),
            "metadata": dict(row.metadata_json or {}),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
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
