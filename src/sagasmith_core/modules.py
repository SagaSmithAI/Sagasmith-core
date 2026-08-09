"""Adventure-module parsing, ingestion, search, and scene progress."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.content_pack import validate_content_package
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
from sagasmith_core.integrity import json_sha256, unique_retired_source_key
from sagasmith_core.models import (
    Campaign,
    Character,
    ModuleActorBinding,
    ModuleAsset,
    ModuleChapter,
    ModuleChunk,
    ModuleContentReview,
    ModuleScene,
    ModuleSource,
    SceneProgress,
    VectorIndexJob,
)
from sagasmith_core.parsing import MarkdownHierarchyParser, ParsedChunk
from sagasmith_core.portable import build_actor_card
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
from sagasmith_core.visibility import MODULE_VISIBILITY_SCOPES

MANAGED_MODULE_SOURCE_FIELDS = frozenset({"module_id", "scene_id", "chunk_id", "content_sha256"})
EXACT_MODULE_SOURCE_FIELD_ORDER = (
    "module_id",
    "scene_id",
    "chunk_id",
    "page_start",
    "page_end",
    "heading_path",
    "content_sha256",
)
EXACT_MODULE_SOURCE_FIELDS = frozenset(EXACT_MODULE_SOURCE_FIELD_ORDER)
_SOURCE_EVIDENCE_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
    }
)


def canonical_heading_path(headings: Sequence[str]) -> tuple[str, ...]:
    """Return one stable heading path, collapsing parser-boundary duplicates."""

    normalized: list[str] = []
    for raw_heading in headings:
        heading = str(raw_heading).strip()
        if not heading:
            continue
        if normalized and normalized[-1].casefold() == heading.casefold():
            continue
        normalized.append(heading)
    return tuple(normalized)


def _portable_scene_chunks(
    scene: ModuleScene,
    chunks: Sequence[ModuleChunk],
    chapter_title: str,
) -> list[dict[str, Any]]:
    """Export real scene text when an imported structural scene has no chunk rows."""

    if chunks:
        return [
            {
                "ordinal": chunk.ordinal,
                "heading_path": list(chunk.heading_path),
                "content": chunk.content,
                "start_offset": chunk.char_start,
                "end_offset": chunk.char_end,
                "metadata": dict(chunk.metadata_json or {}),
                "content_hash": chunk.content_hash
                or hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
            }
            for chunk in chunks
        ]
    headings = list(canonical_heading_path([chapter_title, *scene.headings, scene.title]))
    return [
        {
            "ordinal": 0,
            "heading_path": headings,
            "content": scene.content,
            "start_offset": 0,
            "end_offset": len(scene.content),
            "metadata": {"derived_from_scene_content": True},
            "content_hash": hashlib.sha256(scene.content.encode("utf-8")).hexdigest(),
        }
    ]


def clean_source_evidence_text(value: Any) -> str:
    """Remove PDF artifacts and normalize typography while preserving letter case."""

    text = str(value or "").replace("\x02", "").replace("\u00ad", "")
    text = re.sub(r"\ufffe[ \t\r\n]*", "", text)
    return " ".join(text.translate(_SOURCE_EVIDENCE_TRANSLATION).split())


def normalize_source_evidence_text(value: Any) -> str:
    """Normalize indexed PDF text for exact, case-insensitive containment checks."""

    return clean_source_evidence_text(value).casefold()


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
        page_locator = PageLocator(content)
        if not headings:
            return [
                self._chapter(
                    0,
                    "Document",
                    content,
                    0,
                    len(content),
                    page_locator,
                )
            ]
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
            parsed.append(
                self._chapter(
                    0,
                    "Front Matter",
                    preamble,
                    0,
                    first_chapter_start,
                    page_locator,
                )
            )
        for ordinal, (_heading_index, heading) in enumerate(chapter_starts):
            start = structural_starts[ordinal]
            end = (
                structural_starts[ordinal + 1]
                if ordinal + 1 < len(chapter_starts)
                else len(content)
            )
            title = heading.group(2).strip()
            parsed.append(
                self._chapter(
                    len(parsed),
                    title,
                    content[start:end],
                    start,
                    end,
                    page_locator,
                )
            )
        return parsed

    def document_metadata(self, content: str) -> dict[str, Any]:
        factory = getattr(self.profile, "document_metadata", None)
        return dict(factory(content)) if callable(factory) else {}

    @staticmethod
    def _structural_start(content: str, heading: re.Match[str]) -> int:
        """Keep a page marker immediately preceding a chapter with that chapter."""
        cursor = heading.start()
        while cursor and content[cursor - 1].isspace():
            cursor -= 1
        line_start = content.rfind("\n", 0, cursor) + 1
        candidate = content[line_start:cursor]
        return line_start if re.fullmatch(r"<!-- page: \d+ -->", candidate) else heading.start()

    @staticmethod
    def _last_content_offset(content: str, start: int, end: int) -> int:
        """Skip whitespace and trailing page markers when resolving an inclusive end."""
        cursor = end
        while cursor > start:
            while cursor > start and content[cursor - 1].isspace():
                cursor -= 1
            line_start = max(start, content.rfind("\n", start, cursor) + 1)
            if re.fullmatch(r"<!-- page: \d+ -->", content[line_start:cursor]):
                cursor = line_start
                continue
            break
        return max(start, cursor - 1)

    def _chapter(
        self,
        ordinal: int,
        title: str,
        chapter_content: str,
        global_start: int,
        global_end: int,
        page_locator: PageLocator,
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
            raw = chapter_content[start:end]
            clean = strip_page_markers(raw).strip()
            sections = self.hierarchy_parser.parse(raw)
            chunks = []
            for section in sections:
                for chunk in section.chunks:
                    text = strip_page_markers(chunk.content)
                    if not text:
                        continue
                    local_start = start + chunk.start_offset
                    local_end = start + chunk.end_offset
                    absolute_start = global_start + local_start
                    absolute_end = global_start + local_end
                    metadata = {
                        **chunk.metadata,
                        "start_line": chapter_content.count("\n", 0, local_start) + 1,
                        "end_line": chapter_content.count("\n", 0, local_end) + 1,
                        "page_start": page_locator.page_for_offset(absolute_start),
                        "page_end": page_locator.page_for_offset(
                            global_start
                            + self._last_content_offset(
                                chapter_content,
                                local_start,
                                local_end,
                            )
                        ),
                        "chunk_type": self.profile.classify_chunk(section.title, text),
                        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "absolute_start": absolute_start,
                        "absolute_end": absolute_end,
                    }
                    chunks.append(
                        type(chunk)(
                            ordinal=len(chunks),
                            heading_path=canonical_heading_path((title, *chunk.heading_path)),
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
                    heading_path=canonical_heading_path((title, scene_title)),
                    chunks=tuple(chunks),
                    metadata={
                        **boundary.metadata,
                        "start_line": chapter_content.count("\n", 0, start) + 1,
                        "end_line": chapter_content.count("\n", 0, end) + 1,
                        "page_start": page_locator.page_for_offset(scene_start),
                        "page_end": page_locator.page_for_offset(
                            global_start
                            + self._last_content_offset(
                                chapter_content,
                                start,
                                end,
                            )
                        ),
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
                "page_start": page_locator.page_for_offset(global_start),
                "page_end": page_locator.page_for_offset(
                    global_start
                    + self._last_content_offset(
                        chapter_content,
                        0,
                        len(chapter_content),
                    )
                ),
                "absolute_start": global_start,
                "absolute_end": global_end,
            },
        )


@dataclass(frozen=True)
class _PortableModuleProfile:
    """Preserve the source parser identity while replaying stored structure."""

    name: str
    version: str

    @staticmethod
    def document_metadata(_content: str) -> dict[str, Any]:
        return {}


class _ContentModuleParser:
    """Replay a unified module's stored source spans and Scene Atlas."""

    def __init__(self, package: dict[str, Any], documents: Mapping[str, str]) -> None:
        self.package = package
        self.documents = dict(documents)
        self.profile = _PortableModuleProfile(name="content-package", version="1")

    def document_metadata(self, _content: str) -> dict[str, Any]:
        return {}

    def parse(self, content: str) -> list[ParsedChapter]:
        scenes = list(self.package["content"]["scene_atlas"])
        sources = {str(source["source_key"]): source for source in self.package["sources"]}
        chapters: dict[int, list[dict[str, Any]]] = {}
        for scene in scenes:
            chapters.setdefault(int(scene["chapter_ordinal"]), []).append(scene)
        parsed = []
        for chapter_ordinal in sorted(chapters):
            rows = sorted(chapters[chapter_ordinal], key=lambda item: int(item["scene_ordinal"]))
            parsed_scenes = []
            for scene in rows:
                span = dict(scene["source_span"])
                source_key = str(span["source_key"])
                document = self.documents[source_key]
                start = int(span["start_offset"])
                end = int(span["end_offset"])
                scene_content = document[start:end]
                source = sources[source_key]
                chunks = [
                    chunk
                    for section in source["sections"]
                    if int(section["start_offset"]) >= start and int(section["end_offset"]) <= end
                    for chunk in section["chunks"]
                ]
                metadata = {
                    **dict(scene.get("metadata") or {}),
                    "stable_key": scene["stable_key"],
                    "scene_type": scene["scene_type"],
                    "page_start": scene.get("page_start"),
                    "page_end": scene.get("page_end"),
                    "headings": list(scene.get("headings") or []),
                    "keywords": list(scene.get("keywords") or []),
                }
                parsed_scenes.append(
                    ParsedScene(
                        ordinal=int(scene["scene_ordinal"]),
                        title=str(scene["title"]),
                        content=scene_content,
                        heading_path=tuple(str(item) for item in scene.get("headings") or []),
                        chunks=tuple(
                            ParsedChunk(
                                ordinal=int(chunk["ordinal"]),
                                heading_path=tuple(str(item) for item in chunk["heading_path"]),
                                content=document[
                                    int(chunk["start_offset"]) : int(chunk["end_offset"])
                                ],
                                start_offset=int(chunk["start_offset"]),
                                end_offset=int(chunk["end_offset"]),
                                metadata={
                                    **dict(chunk.get("metadata") or {}),
                                    "page_start": chunk.get("page_start"),
                                    "page_end": chunk.get("page_end"),
                                    "content_hash": chunk["content_hash"],
                                    "portable_chunk_key": chunk["key"],
                                },
                            )
                            for chunk in chunks
                        ),
                        metadata=metadata,
                    )
                )
            parsed.append(
                ParsedChapter(
                    ordinal=chapter_ordinal,
                    title=str(rows[0]["chapter"]),
                    content="\n\n".join(scene.content for scene in parsed_scenes),
                    scenes=tuple(parsed_scenes),
                    metadata={},
                )
            )
        return parsed


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
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
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
            else (f"{logical_key}--staged-{checksum[:12]}-{parser_profile}-{parser_version}")
        )
        with self.database.transaction() as session:
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(
                session,
                idempotency_key,
                idempotency_write,
            )
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
                result = ModuleIngestResult(existing.id, True, *counts, 0)
                idempotency.remember_write_in_session(
                    session,
                    campaign_id=campaign_id,
                    key=idempotency_key,
                    write=idempotency_write,
                    result=result,
                )
                return result
            if existing and activate:
                # Module text is external to save payloads, but scene progress
                # and historical snapshots hold foreign keys to its scene rows.
                # Retire the old source instead of deleting it: a restore must
                # always be able to resolve the exact scene it captured.
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
                # Activation is applied below through the same transaction used
                # by activate_candidate().  Creating an already-active row here
                # used to bypass logical-key replacement when source_key changed.
                active=False,
                parser_profile=parser_profile,
                parser_version=parser_version,
                warnings=list(normalized_document.warnings) if normalized_document else [],
                metadata_json={
                    key: value
                    for key, value in {
                        **dict(metadata or {}),
                        **profile_metadata,
                        "logical_source_key": logical_key,
                    }.items()
                    if key != "import_state"
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
                        status="indexed",
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
                    visibility = str(scene_metadata.get("visibility", "keeper"))
                    if visibility not in MODULE_VISIBILITY_SCOPES:
                        raise ValueError(
                            f"invalid module scene visibility {visibility!r}: "
                            f"expected one of {sorted(MODULE_VISIBILITY_SCOPES)}"
                        )
                    scene_metadata["visibility"] = visibility
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
                            vector_metadata = {
                                "system_id": campaign.system_id,
                                "campaign_id": campaign_id,
                                "module_id": module_id,
                                "scene_id": scene_id,
                            }
                            session.add(
                                VectorIndexJob(
                                    id=job_id,
                                    system_id=campaign.system_id,
                                    collection="modules",
                                    entity_type="module_chunk",
                                    entity_id=chunk_id,
                                    payload={
                                        "document": chunk.content,
                                        "metadata": vector_metadata,
                                        "embedding_model": embedder.model_name,
                                    },
                                )
                            )
                    scene_count += 1
            if activate:
                self._activate_candidate_in_session(
                    session,
                    campaign_id=campaign_id,
                    row=source_row,
                    explicit_remaps={},
                )
            result = ModuleIngestResult(
                module_id,
                False,
                len(parsed),
                scene_count,
                chunk_count,
                embedding_count,
            )
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

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
        ocr_provider: OcrProvider | None = None,
        document_cache_dir: str | Path | None = None,
        expected_checksum: str | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
        layout_profile: DocumentLayoutProfile = GENERIC_DOCUMENT_LAYOUT_PROFILE,
        page_revisions: Sequence[Mapping[str, Any]] | None = None,
    ) -> ModuleIngestResult:
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
            idempotency_key=idempotency_key,
            idempotency_write=idempotency_write,
        )

    def inspect_path(
        self,
        path: str | Path,
        *,
        parser: MarkdownModuleParser | None = None,
        ocr_provider: OcrProvider | None = None,
        document_cache_dir: str | Path | None = None,
        expected_checksum: str | None = None,
        layout_profile: DocumentLayoutProfile = GENERIC_DOCUMENT_LAYOUT_PROFILE,
        page_revisions: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        document = normalize_document(
            path,
            ocr_provider=ocr_provider,
            cache_dir=document_cache_dir,
            expected_checksum=expected_checksum,
            layout_profile=layout_profile,
        )
        document = apply_document_page_revisions(document, page_revisions)
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
        ocr_provider: OcrProvider | None = None,
        document_cache_dir: str | Path | None = None,
        expected_checksum: str | None = None,
        layout_profile: DocumentLayoutProfile = GENERIC_DOCUMENT_LAYOUT_PROFILE,
        page_revisions: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Parse a module without persistence and expose stable scene/package evidence."""
        document = normalize_document(
            path,
            ocr_provider=ocr_provider,
            cache_dir=document_cache_dir,
            expected_checksum=expected_checksum,
            layout_profile=layout_profile,
        )
        document = apply_document_page_revisions(document, page_revisions)
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
                visibility = str(metadata.get("visibility", "keeper"))
                if visibility not in MODULE_VISIBILITY_SCOPES:
                    errors.append(f"scene {stable_key} has invalid visibility {visibility!r}")
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
                            f"scene {stable_key} has invalid PDF page range {page_start}-{page_end}"
                        )
                scenes.append(
                    {
                        "stable_key": stable_key,
                        "chapter": chapter.title,
                        "chapter_ordinal": chapter.ordinal,
                        "ordinal": scene.ordinal,
                        "title": scene.title,
                        "headings": list(canonical_heading_path(scene.heading_path)),
                        "scene_type": metadata.get("scene_type", "section"),
                        "visibility": visibility,
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
                    "runtime_manifest": dict(row.metadata_json or {}).get("runtime_manifest"),
                    "portable_package": dict(row.metadata_json or {}).get("portable_package"),
                    "chapters": self._counts(session, row.id)[0],
                    "scenes": self._counts(session, row.id)[1],
                    "chunks": self._counts(session, row.id)[2],
                }
                for row in rows
            ]

    def export_content_descriptor(
        self,
        campaign_id: str,
        module_id: str,
        *,
        portable_id: str,
        version: str = "1.0.0",
        actors: Sequence[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        dependencies: Sequence[dict[str, Any]] | None = None,
        asset_loader: Callable[[str], bytes] | None = None,
        blob_sink: Callable[[str, bytes], None] | None = None,
        manifest: dict[str, Any] | None = None,
        catalogs: dict[str, Any] | None = None,
        narrative: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Export one module revision for direct normalization into a v2 Pack.

        Runtime ids are replaced by scene stable keys, asset content keys, and
        chunk hashes.  Actor knowledge, scene progress, snapshots, and other
        campaign state are intentionally outside this authoring package.
        """

        with self.database.transaction() as session:
            source = session.get(ModuleSource, module_id)
            if source is None or source.campaign_id != campaign_id:
                raise LookupError(module_id)
            chapters = list(
                session.scalars(
                    select(ModuleChapter)
                    .where(ModuleChapter.module_id == module_id)
                    .order_by(ModuleChapter.ordinal, ModuleChapter.id)
                )
            )
            scenes = list(
                session.execute(
                    select(ModuleScene, ModuleChapter)
                    .join(ModuleChapter, ModuleChapter.id == ModuleScene.chapter_id)
                    .where(ModuleScene.module_id == module_id)
                    .order_by(ModuleChapter.ordinal, ModuleScene.ordinal, ModuleScene.id)
                )
            )
            if not scenes:
                raise ValueError("cannot export a module without scenes")
            chunks = {
                chunk.id: chunk
                for chunk in session.scalars(
                    select(ModuleChunk).where(ModuleChunk.module_id == module_id)
                )
            }
            chunks_by_scene: dict[str, list[ModuleChunk]] = {}
            for chunk in chunks.values():
                chunks_by_scene.setdefault(chunk.scene_id, []).append(chunk)
            for scene_chunks in chunks_by_scene.values():
                scene_chunks.sort(key=lambda item: (item.ordinal, item.id))
            scenes = [(scene, chapter) for scene, chapter in scenes if scene.content.strip()]
            if not scenes:
                raise ValueError("cannot export a module without content-bearing scenes")
            scene_keys = {
                scene.id: str(dict(scene.metadata_json or {}).get("stable_key") or "")
                for scene, _chapter in scenes
            }
            if any(not key for key in scene_keys.values()):
                raise ValueError("cannot export a module with missing scene stable keys")
            exported_scene_keys = set(scene_keys.values())
            assets = list(
                session.scalars(
                    select(ModuleAsset)
                    .where(ModuleAsset.module_id == module_id)
                    .order_by(ModuleAsset.created_at, ModuleAsset.id)
                )
            )
            asset_keys = {
                asset.id: f"asset-{asset.checksum[:16]}-{index + 1}"
                for index, asset in enumerate(assets)
            }
            asset_payload: list[dict[str, Any]] = []
            for asset in assets:
                content: bytes | None = None
                if asset_loader is not None:
                    try:
                        content = asset_loader(asset.source_path)
                    except (LookupError, OSError):
                        content = None
                if content is None and asset.normalized_content is not None:
                    normalized_bytes = asset.normalized_content.encode("utf-8")
                    if hashlib.sha256(normalized_bytes).hexdigest() == asset.checksum:
                        content = normalized_bytes
                if content is not None and hashlib.sha256(content).hexdigest() != asset.checksum:
                    raise ValueError(
                        f"module asset checksum mismatch while exporting: {asset.source_path}"
                    )
                if content is None:
                    raise ValueError(
                        "module asset bytes are unavailable; provide asset_loader for a "
                        f"self-contained export: {asset.source_path}"
                    )
                if blob_sink is not None:
                    blob_sink(asset.checksum, content)
                asset_payload.append(
                    {
                        "asset_key": asset_keys[asset.id],
                        "name": Path(asset.source_path).name or asset_keys[asset.id],
                        "media_type": asset.media_type,
                        "checksum": asset.checksum,
                        "size": len(content) if content is not None else 0,
                        "blob_key": f"blobs/sha256/{asset.checksum}",
                        "normalized_content": asset.normalized_content,
                        "metadata": dict(asset.metadata_json or {}),
                    }
                )

            reviews = list(
                session.scalars(
                    select(ModuleContentReview)
                    .where(ModuleContentReview.module_id == module_id)
                    .order_by(ModuleContentReview.created_at, ModuleContentReview.id)
                )
            )
            review_payload = []
            for review in reviews:
                if review.scene_id not in scene_keys:
                    continue
                evidence = dict(review.evidence_json or {})
                if evidence.get("asset_id"):
                    asset_key = asset_keys.get(str(evidence["asset_id"]))
                    if asset_key is None:
                        raise ValueError("content review refers to an unknown module asset")
                    portable_evidence = {
                        "asset_key": asset_key,
                        "page": evidence.get("page"),
                        "reviewer": evidence.get("reviewer"),
                        "observation": evidence.get("observation"),
                    }
                else:
                    chunk_ids = list(evidence.get("source_chunk_ids") or [])
                    if not chunk_ids or any(chunk_id not in chunks for chunk_id in chunk_ids):
                        raise ValueError("content review refers to unknown module chunks")
                    portable_evidence = {
                        "chunk_hashes": list(
                            dict.fromkeys(
                                chunks[chunk_id].content_hash
                                or hashlib.sha256(
                                    chunks[chunk_id].content.encode("utf-8")
                                ).hexdigest()
                                for chunk_id in chunk_ids
                            )
                        ),
                        "reviewer": evidence.get("reviewer"),
                        "observation": evidence.get("observation"),
                    }
                review_payload.append(
                    {
                        "scene_key": scene_keys[review.scene_id],
                        "content_key": review.content_key,
                        "content_kind": review.content_kind,
                        "normalized_content": review.normalized_content,
                        "evidence": portable_evidence,
                        "metadata": dict(review.metadata_json or {}),
                    }
                )

            normalized_documents = [
                asset.normalized_content
                for asset in assets
                if asset.normalized_content
                and asset.media_type in {"application/pdf", "text/markdown", "text/plain"}
            ]
            document_content = (
                normalized_documents[0]
                if normalized_documents
                else "\n\n".join(chapter.content for chapter in chapters).strip()
            )
            if not document_content:
                raise ValueError("cannot export a module without normalized source content")
            source_metadata = {
                key: value
                for key, value in dict(source.metadata_json or {}).items()
                if key not in {"import_state", "source_path"}
            }
            source_key = str(source_metadata.get("logical_source_key") or source.source_key)
            scene_atlas = [
                {
                    "stable_key": scene_keys[scene.id],
                    "title": scene.title,
                    "chapter": chapter.title,
                    "chapter_ordinal": chapter.ordinal,
                    "scene_ordinal": scene.ordinal,
                    "scene_type": scene.scene_type,
                    "page_start": scene.page_start,
                    "page_end": scene.page_end,
                    "headings": list(scene.headings),
                    "keywords": list(scene.keywords),
                    "content": scene.content,
                    "chunks": _portable_scene_chunks(
                        scene,
                        chunks_by_scene.get(scene.id, []),
                        chapter.title,
                    ),
                    "metadata": {
                        key: value
                        for key, value in dict(scene.metadata_json or {}).items()
                        if key not in {"stable_key", "content_checksum"}
                    },
                    "content_checksum": hashlib.sha256(scene.content.encode("utf-8")).hexdigest(),
                }
                for scene, chapter in scenes
            ]
            if actors is None:
                actor_rows = list(
                    session.execute(
                        select(ModuleActorBinding, Character)
                        .join(Character, Character.id == ModuleActorBinding.character_id)
                        .where(ModuleActorBinding.module_id == module_id)
                        .order_by(
                            ModuleActorBinding.portable_actor_id,
                            ModuleActorBinding.scene_key,
                            ModuleActorBinding.id,
                        )
                    )
                )
                grouped: dict[str, tuple[Character, list[ModuleActorBinding]]] = {}
                for binding, character in actor_rows:
                    if binding.portable_actor_id in grouped:
                        grouped[binding.portable_actor_id][1].append(binding)
                    else:
                        grouped[binding.portable_actor_id] = (character, [binding])
                actors = [
                    build_actor_card(
                        portable_id=portable_actor_id,
                        version=version,
                        system_id=character.system_id,
                        actor_type=character.character_type,
                        name=character.name,
                        player_name=character.player_name,
                        summary=character.summary,
                        sheet=dict(character.sheet),
                        notes=dict(character.notes),
                        provenance=dict(bindings[0].metadata_json or {}).get(
                            "portable_provenance", {}
                        ),
                        metadata=dict(bindings[0].metadata_json or {}).get("portable_metadata", {}),
                        dependencies=dict(bindings[0].metadata_json or {}).get(
                            "portable_dependencies", []
                        ),
                        bindings=[
                            {
                                "kind": "module_scene" if binding.scene_key else "module",
                                "module_key": source_key,
                                **({"scene_key": binding.scene_key} if binding.scene_key else {}),
                                "binding_kind": binding.binding_kind,
                                "role": binding.role,
                                "metadata": {
                                    key: value
                                    for key, value in dict(binding.metadata_json or {}).items()
                                    if not key.startswith("portable_")
                                },
                            }
                            for binding in bindings
                            if not binding.scene_key or binding.scene_key in exported_scene_keys
                        ],
                    )
                    for portable_actor_id, (character, bindings) in grouped.items()
                ]
            module_manifest = manifest or {
                "title": source.title,
                "classification": str(source_metadata.get("module_classification") or "adventure"),
                "compatibility": {
                    "editions": list(source_metadata.get("editions") or []),
                    "required_capabilities": ["module_pack_v2"],
                },
                "play_profile": {
                    "party_size": {"minimum": None, "maximum": None, "source_refs": []},
                    "starting_level": {"value": None, "source_refs": []},
                    "expected_end_level": {"value": None, "source_refs": []},
                    "advancement": {
                        "modes": ["unknown"],
                        "recommended": "unknown",
                        "source_refs": [],
                    },
                    "pregenerated_characters": {
                        "available": False,
                        "applicability": "Not reviewed",
                        "source_refs": [],
                    },
                },
                "continuity": {
                    "series_id": source_metadata.get("series_id"),
                    "order": source_metadata.get("series_order"),
                    "continues_from": source_metadata.get("continues_from"),
                    "state_policy": dict(source_metadata.get("state_policy") or {}),
                },
                "activation": {"mode": "campaign_attach", "default_active": False},
                "content_summary": {},
            }
            normalized_catalogs = catalogs or {
                "items": [],
                "encounters": [],
                "hazards": [],
                "handouts": [],
                "mechanics": [],
            }
            normalized_narrative = narrative or {"dossiers": [], "endings": []}
            module_manifest["content_summary"] = {
                "scenes": len(scene_atlas),
                "assets": len(asset_payload),
                "content_reviews": len(review_payload),
                "actors": len(actors),
                "catalog_entries": sum(len(items) for items in normalized_catalogs.values()),
                "dossiers": len(normalized_narrative["dossiers"]),
                "endings": len(normalized_narrative["endings"]),
            }
            return {
                "id": portable_id,
                "version": version,
                "system_id": source.system_id,
                "manifest": module_manifest,
                "source": {
                    "source_key": source_key,
                    "title": source.title,
                    "parser_profile": source.parser_profile,
                    "parser_version": source.parser_version,
                    "metadata": source_metadata,
                },
                "document": {
                    "media_type": "text/markdown",
                    "content": document_content,
                    "checksum": hashlib.sha256(document_content.encode("utf-8")).hexdigest(),
                },
                "scene_atlas": scene_atlas,
                "assets": asset_payload,
                "content_reviews": review_payload,
                "actors": list(actors),
                "catalogs": normalized_catalogs,
                "narrative": normalized_narrative,
                "metadata": dict(metadata or {}),
                "dependencies": list(dependencies or []),
            }

    def import_content_package(
        self,
        campaign_id: str,
        package: dict[str, Any],
        blobs: Mapping[str, bytes],
        *,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
        activate: bool = False,
        asset_writer: Callable[[str, dict[str, Any], bytes], str] | None = None,
    ) -> dict[str, Any]:
        """Import a unified module and remap only stable logical references."""

        value = validate_content_package(package)
        if value["kind"] != "module":
            raise ValueError("content package kind must be module")
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            if campaign.system_id != value["system_id"]:
                raise ValueError("content package and campaign must use the same system_id")
        assets_by_key = {asset["asset_key"]: asset for asset in value["assets"]}
        documents: dict[str, str] = {}
        for source in value["sources"]:
            asset = assets_by_key[source["normalized_document_asset_key"]]
            documents[source["source_key"]] = blobs[asset["checksum"]].decode("utf-8")
        primary = value["sources"][0]
        document = documents[primary["source_key"]]
        result = self.ingest(
            campaign_id=campaign_id,
            source_key=primary["source_key"],
            logical_source_key=primary["source_key"],
            title=primary["title"],
            content=document,
            metadata={
                **dict(primary.get("metadata") or {}),
                "content_package": {
                    "id": value["id"],
                    "version": value["version"],
                    "checksum": value["checksum"],
                },
            },
            parser=_ContentModuleParser(value, documents),
            embedder=embedder,
            vector_store=vector_store,
            activate=activate,
        )
        scene_rows = self.scene_index(campaign_id, module_id=result.module_id)
        scene_map = {str(scene["stable_key"]): str(scene["scene_id"]) for scene in scene_rows}
        asset_map: dict[str, str] = {}
        for asset in value["assets"]:
            if asset["kind"] == "normalized_document":
                continue
            if asset_writer is None:
                raise ValueError("asset_writer is required for package assets")
            content = blobs[asset["checksum"]]
            path = asset_writer(result.module_id, dict(asset), content)
            registered = self.register_asset(
                campaign_id=campaign_id,
                module_id=result.module_id,
                source_path=path,
                media_type=asset["media_type"],
                checksum=asset["checksum"],
                metadata={
                    **dict(asset["metadata"]),
                    "content_asset_key": asset["asset_key"],
                    "asset_kind": asset["kind"],
                },
            )
            asset_map[asset["asset_key"]] = registered["id"]
        chunks = self.list_chunks(campaign_id, result.module_id)
        chunk_map = {
            str(dict(chunk.get("metadata") or {}).get("portable_chunk_key") or ""): chunk["id"]
            for chunk in chunks
            if str(dict(chunk.get("metadata") or {}).get("portable_chunk_key") or "")
        }
        packaged_chunks: dict[str, tuple[str, dict[str, Any]]] = {}
        for scene in value["content"]["scene_atlas"]:
            scene_key = str(scene["stable_key"])
            referenced_keys = {str(ref["chunk_key"]) for ref in scene.get("source_refs") or []}
            for source in value["sources"]:
                for section in source["sections"]:
                    for descriptor in section["chunks"]:
                        key = str(descriptor["key"])
                        if key in referenced_keys:
                            packaged_chunks[key] = (scene_key, dict(descriptor))

        chunks_by_scene_and_hash: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for chunk in chunks:
            content_hash = (
                str(chunk.get("content_hash") or "")
                or hashlib.sha256(str(chunk["content"]).encode("utf-8")).hexdigest()
            )
            chunks_by_scene_and_hash.setdefault((str(chunk["scene_id"]), content_hash), []).append(
                chunk
            )

        def imported_chunk_id(chunk_key: str) -> str:
            direct = chunk_map.get(chunk_key)
            if direct is not None:
                return str(direct)
            target = packaged_chunks.get(chunk_key)
            if target is None:
                raise ValueError("content review chunk evidence does not resolve inside the Pack")
            scene_key, descriptor = target
            candidates = chunks_by_scene_and_hash.get(
                (
                    scene_map[scene_key],
                    str(descriptor["content_hash"]),
                ),
                [],
            )
            ordinal = int(descriptor["ordinal"])
            matching = [chunk for chunk in candidates if int(chunk.get("ordinal", -1)) == ordinal]
            resolved = matching[0] if matching else (candidates[0] if candidates else None)
            if resolved is None:
                raise ValueError(
                    "content review chunk evidence could not be remapped from the Pack"
                )
            return str(resolved["id"])

        review_ids = []
        for review in value["content_reviews"]:
            target = dict(review["target"])
            common = {
                "campaign_id": campaign_id,
                "module_id": result.module_id,
                "scene_id": scene_map[str(target["scene_key"])],
                "content_key": str(target["content_key"]),
                "content_kind": str(review["kind"]),
                "normalized_content": str(review["normalized_content"]),
                "reviewer": str(
                    dict(review.get("review") or {}).get("reviewer") or "content-package"
                ),
                "observation": str(
                    dict(review.get("review") or {}).get("observation")
                    or "Imported source-backed review"
                ),
                "metadata": dict(review.get("metadata") or {}),
            }
            evidence = dict(review.get("evidence") or {})
            if evidence.get("asset_key"):
                imported = self.review_content(
                    **common,
                    source_asset_id=asset_map[str(evidence["asset_key"])],
                    page_number=int(evidence["page"]),
                )
            else:
                source_chunk_ids = [
                    imported_chunk_id(str(ref["chunk_key"])) for ref in review["source_refs"]
                ]
                imported = self.review_content(
                    **common,
                    source_chunk_ids=source_chunk_ids,
                )
            review_ids.append(imported["id"])
        return {
            "module_id": result.module_id,
            "skipped": result.skipped,
            "package": {
                "id": value["id"],
                "version": value["version"],
                "checksum": value["checksum"],
            },
            "scene_map": scene_map,
            "asset_map": asset_map,
            "content_review_ids": review_ids,
            "actors": list(value["actors"]),
            "manifest": dict(value["manifest"]),
        }

    def bind_actor(
        self,
        *,
        campaign_id: str,
        module_id: str,
        character_id: str,
        portable_actor_id: str,
        binding_kind: str,
        role: str = "",
        scene_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Associate a local actor with a module using a portable logical identity."""

        portable_id = str(portable_actor_id).strip()
        kind = str(binding_kind).strip().casefold()
        role_value = str(role).strip()
        if not portable_id or len(portable_id) > 200:
            raise ValueError("portable_actor_id must contain 1 to 200 characters")
        if kind not in {"cast", "encounter", "preset_pc"}:
            raise ValueError("binding_kind must be cast, encounter, or preset_pc")
        if len(role_value) > 200:
            raise ValueError("module actor role must not exceed 200 characters")
        with self.database.transaction() as session:
            source = session.get(ModuleSource, module_id)
            if source is None or source.campaign_id != campaign_id:
                raise LookupError(module_id)
            character = session.get(Character, character_id)
            if character is None:
                raise LookupError(character_id)
            if character.system_id != source.system_id or character.campaign_id not in {
                None,
                campaign_id,
            }:
                raise ValueError("module actor must be a same-system library or campaign actor")
            scene_key = ""
            if scene_id is not None:
                scene = session.get(ModuleScene, scene_id)
                if scene is None or scene.module_id != module_id:
                    raise ValueError("module actor scene must belong to the module")
                scene_key = str(dict(scene.metadata_json or {}).get("stable_key") or "")
                if not scene_key:
                    raise ValueError("module actor scene has no stable key")
            existing = session.scalar(
                select(ModuleActorBinding).where(
                    ModuleActorBinding.module_id == module_id,
                    ModuleActorBinding.scene_key == scene_key,
                    ModuleActorBinding.character_id == character_id,
                    ModuleActorBinding.binding_kind == kind,
                    ModuleActorBinding.role == role_value,
                )
            )
            if existing is None:
                existing = ModuleActorBinding(
                    id=str(uuid.uuid4()),
                    module_id=module_id,
                    scene_id=scene_id,
                    scene_key=scene_key,
                    character_id=character_id,
                    portable_actor_id=portable_id,
                    binding_kind=kind,
                    role=role_value,
                    metadata_json=dict(metadata or {}),
                )
                session.add(existing)
            elif existing.portable_actor_id != portable_id:
                raise ValueError("module actor binding has a different portable_actor_id")
            else:
                existing.metadata_json = {
                    **dict(existing.metadata_json or {}),
                    **dict(metadata or {}),
                }
            session.flush()
            return self._actor_binding_view(existing)

    def list_actor_bindings(
        self,
        campaign_id: str,
        module_id: str,
        *,
        scene_id: str | None = None,
        binding_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """List module cast, encounter, and preset-PC associations."""

        with self.database.transaction() as session:
            source = session.get(ModuleSource, module_id)
            if source is None or source.campaign_id != campaign_id:
                raise LookupError(module_id)
            statement = (
                select(ModuleActorBinding, Character)
                .join(Character, Character.id == ModuleActorBinding.character_id)
                .where(ModuleActorBinding.module_id == module_id)
                .order_by(
                    ModuleActorBinding.scene_key,
                    ModuleActorBinding.binding_kind,
                    ModuleActorBinding.role,
                    Character.name,
                )
            )
            if scene_id is not None:
                statement = statement.where(ModuleActorBinding.scene_id == scene_id)
            if binding_kind is not None:
                statement = statement.where(
                    ModuleActorBinding.binding_kind == str(binding_kind).casefold()
                )
            return [
                {
                    **self._actor_binding_view(row.ModuleActorBinding),
                    "character": {
                        "id": row.Character.id,
                        "name": row.Character.name,
                        "character_type": row.Character.character_type,
                        "campaign_id": row.Character.campaign_id,
                        "template_id": row.Character.template_id,
                    },
                }
                for row in session.execute(statement)
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
        normalized_content: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> dict[str, Any]:
        """Idempotently register a managed derived module asset."""
        resolved = str(Path(source_path).expanduser().resolve())
        with self.database.transaction() as session:
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(
                session,
                idempotency_key,
                idempotency_write,
            )
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
                    normalized_content=normalized_content,
                    metadata_json=dict(metadata or {}),
                )
                session.add(row)
            elif row.checksum != checksum:
                raise ValueError("managed module asset path has different content")
            else:
                row.media_type = media_type
                if normalized_content is not None:
                    row.normalized_content = normalized_content
                row.metadata_json = {**dict(row.metadata_json or {}), **dict(metadata or {})}
            session.flush()
            result = self._asset_view(row)
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

    def review_content(
        self,
        *,
        campaign_id: str,
        module_id: str,
        scene_id: str,
        content_key: str,
        content_kind: str,
        normalized_content: str,
        source_asset_id: str | None = None,
        page_number: int | None = None,
        source_chunk_ids: Sequence[str] | None = None,
        reviewer: str,
        observation: str,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
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
        chunk_ids = list(dict.fromkeys(str(item) for item in source_chunk_ids or [] if str(item)))
        visual_evidence = source_asset_id is not None or page_number is not None
        text_evidence = bool(chunk_ids)
        if visual_evidence == text_evidence:
            raise ValueError(
                "content review requires exactly one evidence mode: source asset/page or chunks"
            )
        if visual_evidence and (
            not source_asset_id
            or isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
        ):
            raise ValueError("visual content review requires a PDF/image asset and 1-based page")
        if not reviewer_value:
            raise ValueError("reviewer is required")
        if not observation_value or len(observation_value) > 500:
            raise ValueError("observation must contain 1 to 500 characters")
        metadata_value = dict(metadata or {})
        checksum = json_sha256(
            {
                "content_key": key,
                "content_kind": kind,
                "normalized_content": content,
                "metadata": metadata_value,
            }
        )

        with self.database.transaction() as session:
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(
                session,
                idempotency_key,
                idempotency_write,
            )
            source = session.get(ModuleSource, module_id)
            if source is None or source.campaign_id != campaign_id:
                raise LookupError(module_id)
            scene = session.get(ModuleScene, scene_id)
            if scene is None or scene.module_id != module_id:
                raise ValueError("content review scene must belong to the module")
            evidence: dict[str, Any]
            if visual_evidence:
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
                        raise ValueError(
                            "content review page must match rendered asset source_page"
                        )
                evidence = {
                    "asset_id": asset.id,
                    "asset_checksum": asset.checksum,
                    "page": page_number,
                    "reviewer": reviewer_value,
                    "observation": observation_value,
                    "confidence": "reviewed_image",
                }
            else:
                chunk_rows = list(
                    session.scalars(select(ModuleChunk).where(ModuleChunk.id.in_(chunk_ids)))
                )
                chunks_by_id = {row.id: row for row in chunk_rows}
                if len(chunks_by_id) != len(chunk_ids):
                    raise ValueError("content review source chunks were not all found")
                ordered_chunks = [chunks_by_id[chunk_id] for chunk_id in chunk_ids]
                if any(
                    row.module_id != module_id or row.scene_id != scene_id for row in ordered_chunks
                ):
                    raise ValueError("content review source chunks must belong to the module scene")
                page_starts = [
                    row.page_start for row in ordered_chunks if row.page_start is not None
                ]
                page_ends = [row.page_end for row in ordered_chunks if row.page_end is not None]
                evidence = {
                    "source_chunk_ids": chunk_ids,
                    "source_chunk_checksums": {row.id: row.content_hash for row in ordered_chunks},
                    "page_start": min(page_starts) if page_starts else None,
                    "page_end": max(page_ends) if page_ends else None,
                    "reviewer": reviewer_value,
                    "observation": observation_value,
                    "confidence": "reviewed_text",
                }

            existing = session.scalar(
                select(ModuleContentReview).where(
                    ModuleContentReview.module_id == module_id,
                    ModuleContentReview.scene_id == scene_id,
                    ModuleContentReview.content_key == key,
                    ModuleContentReview.checksum == checksum,
                )
            )
            if existing is not None:
                result = self._content_review_view(existing)
                idempotency.remember_write_in_session(
                    session,
                    campaign_id=campaign_id,
                    key=idempotency_key,
                    write=idempotency_write,
                    result=result,
                )
                return result
            row = ModuleContentReview(
                id=str(uuid.uuid4()),
                module_id=module_id,
                scene_id=scene_id,
                content_key=key,
                content_kind=kind,
                normalized_content=content,
                checksum=checksum,
                evidence_json=evidence,
                metadata_json=metadata_value,
            )
            session.add(row)
            session.flush()
            result = self._content_review_view(row)
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

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
            heading_path = list(canonical_heading_path(row.ModuleChunk.heading_path))
            content_sha256 = hashlib.sha256(row.ModuleChunk.content.encode("utf-8")).hexdigest()
            source_ref = {
                "module_id": row.ModuleSource.id,
                "scene_id": row.ModuleScene.id,
                "chunk_id": row.ModuleChunk.id,
                "page_start": row.ModuleChunk.page_start,
                "page_end": row.ModuleChunk.page_end,
                "heading_path": heading_path,
                "content_sha256": content_sha256,
            }
            return {
                "chunk_id": row.ModuleChunk.id,
                "campaign_id": row.ModuleSource.campaign_id,
                "content": row.ModuleChunk.content,
                "content_sha256": content_sha256,
                "source_ref": source_ref,
                "heading_path": heading_path,
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

    def list_chunks(
        self,
        campaign_id: str,
        module_id: str,
        *,
        scene_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return ordered, source-scoped chunks for downstream content review."""
        with self.database.transaction() as session:
            source = session.get(ModuleSource, module_id)
            if source is None or source.campaign_id != campaign_id:
                raise LookupError(module_id)
            statement = (
                select(ModuleChunk, ModuleScene)
                .join(ModuleScene, ModuleScene.id == ModuleChunk.scene_id)
                .where(ModuleChunk.module_id == module_id)
                .order_by(ModuleChunk.ordinal, ModuleChunk.id)
            )
            if scene_id is not None:
                statement = statement.where(ModuleChunk.scene_id == scene_id)
            return [
                {
                    "id": row.ModuleChunk.id,
                    "module_id": module_id,
                    "scene_id": row.ModuleChunk.scene_id,
                    "scene_title": row.ModuleScene.title,
                    "ordinal": row.ModuleChunk.ordinal,
                    "heading_path": list(canonical_heading_path(row.ModuleChunk.heading_path)),
                    "content": row.ModuleChunk.content,
                    "content_hash": row.ModuleChunk.content_hash,
                    "chunk_type": row.ModuleChunk.chunk_type,
                    "page_start": row.ModuleChunk.page_start,
                    "page_end": row.ModuleChunk.page_end,
                }
                for row in session.execute(statement)
            ]

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
                .order_by(ModuleChapter.ordinal, ModuleScene.ordinal, ModuleScene.id)
            )
            if module_id:
                statement = statement.where(ModuleSource.id == module_id)
            else:
                statement = statement.where(ModuleSource.active.is_(True))
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
                        ModuleSource.active.is_(True),
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

    def activate_candidate(
        self,
        campaign_id: str,
        module_id: str,
        *,
        progress_remaps: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> dict[str, Any]:
        """Atomically make one staged revision current for its logical module key."""
        with self.database.transaction() as session:
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(
                session,
                idempotency_key,
                idempotency_write,
            )
            row = session.get(ModuleSource, module_id)
            if row is None or row.campaign_id != campaign_id:
                raise LookupError(module_id)
            explicit_remaps = {
                str(source_scene_id): str(target_scene_id)
                for source_scene_id, target_scene_id in (progress_remaps or {}).items()
            }
            result = self._activate_candidate_in_session(
                session,
                campaign_id=campaign_id,
                row=row,
                explicit_remaps=explicit_remaps,
            )
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

    def _activate_candidate_in_session(
        self,
        session: Any,
        *,
        campaign_id: str,
        row: ModuleSource,
        explicit_remaps: dict[str, str],
    ) -> dict[str, Any]:
        """Apply the sole module-activation policy inside an existing transaction."""

        logical_key = str(dict(row.metadata_json or {}).get("logical_source_key") or row.source_key)
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
        progress_migrations = self._migrate_progress_in_session(
            session,
            campaign_id=campaign_id,
            replaced_module_ids=replaced,
            target_module_id=row.id,
            explicit_remaps=explicit_remaps,
        )
        row.active = True
        row.metadata_json = {
            key: value
            for key, value in dict(row.metadata_json or {}).items()
            if key != "import_state"
        }
        session.flush()
        return {
            "module_id": row.id,
            "active": True,
            "replaced_module_ids": replaced,
            "progress_migrations": progress_migrations,
        }

    @staticmethod
    def _migrate_progress_in_session(
        session: Any,
        *,
        campaign_id: str,
        replaced_module_ids: list[str],
        target_module_id: str,
        explicit_remaps: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Move scene progress to one newly authoritative module revision."""

        target_scenes = list(
            session.scalars(select(ModuleScene).where(ModuleScene.module_id == target_module_id))
        )
        target_by_id = {scene.id: scene for scene in target_scenes}
        target_by_stable_key = {
            str(dict(scene.metadata_json or {}).get("stable_key") or ""): scene
            for scene in target_scenes
            if str(dict(scene.metadata_json or {}).get("stable_key") or "")
        }
        migrations: list[dict[str, Any]] = []
        consumed_explicit_remaps: set[str] = set()
        if replaced_module_ids:
            progress_rows = list(
                session.execute(
                    select(SceneProgress, ModuleScene)
                    .join(ModuleScene, ModuleScene.id == SceneProgress.scene_id)
                    .where(
                        SceneProgress.campaign_id == campaign_id,
                        ModuleScene.module_id.in_(replaced_module_ids),
                    )
                )
            )
            for progress, source_scene in progress_rows:
                stable_key = str(dict(source_scene.metadata_json or {}).get("stable_key") or "")
                target_scene = target_by_stable_key.get(stable_key)
                mode = "stable_key"
                if source_scene.id in explicit_remaps:
                    target_scene = target_by_id.get(explicit_remaps[source_scene.id])
                    consumed_explicit_remaps.add(source_scene.id)
                    mode = "dm_ruling"
                if target_scene is None:
                    raise ValueError(
                        "module activation requires a DM-reviewed progress remap for "
                        f"scene {source_scene.id}"
                    )
                conflict = session.scalar(
                    select(SceneProgress).where(
                        SceneProgress.campaign_id == campaign_id,
                        SceneProgress.scope_id == progress.scope_id,
                        SceneProgress.scene_id == target_scene.id,
                        SceneProgress.id != progress.id,
                    )
                )
                if conflict is not None:
                    raise ValueError(
                        "module activation cannot merge two progress records for "
                        f"scope {progress.scope_id} and scene {target_scene.id}"
                    )
                progress.scene_id = target_scene.id
                migrations.append(
                    {
                        "scope_id": progress.scope_id,
                        "from_scene_id": source_scene.id,
                        "to_scene_id": target_scene.id,
                        "stable_key": stable_key,
                        "mode": mode,
                    }
                )
        unused_remaps = set(explicit_remaps) - consumed_explicit_remaps
        if unused_remaps:
            raise ValueError(
                "progress remaps reference scenes without active progress: "
                + ", ".join(sorted(unused_remaps))
            )
        return migrations

    @staticmethod
    def _scene_stable_keys(parsed: Sequence[ParsedChapter]) -> dict[tuple[int, int], str]:
        """Build deterministic keys and disambiguate repeated semantic headings."""
        result: dict[tuple[int, int], str] = {}
        occurrences: dict[str, int] = {}
        for chapter in parsed:
            for scene in chapter.scenes:
                supplied = str(dict(scene.metadata or {}).get("stable_key") or "").strip()
                base = supplied or ModuleService._scene_stable_key(scene.heading_path, scene.title)
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
        module_ids: Sequence[str] | None = None,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
        query_hints: dict[str, Sequence[str]] | None = None,
    ) -> list[SearchHit]:
        enriched = enrich_query(query, extra_terms=query_hints)
        selected_module_ids = tuple(dict.fromkeys(str(item).strip() for item in (module_ids or ())))
        if module_ids is not None and (
            not selected_module_ids or any(not item for item in selected_module_ids)
        ):
            raise ValueError("module_ids must contain at least one non-empty module id")
        statement = (
            select(ModuleChunk, ModuleScene, ModuleChapter, ModuleSource)
            .join(ModuleScene, ModuleScene.id == ModuleChunk.scene_id)
            .join(ModuleChapter, ModuleChapter.id == ModuleScene.chapter_id)
            .join(ModuleSource, ModuleSource.id == ModuleChunk.module_id)
            .where(
                ModuleSource.campaign_id == campaign_id,
                ModuleSource.active.is_(True),
            )
        )
        if selected_module_ids:
            statement = statement.where(ModuleSource.id.in_(selected_module_ids))
        with self.database.transaction() as session:
            rows = session.execute(statement).all()
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
                VectorIndexJobService(self.database).flush(
                    vector_store,
                    system_id=rows[0].ModuleSource.system_id,
                    collection="modules",
                    profile=getattr(embedder, "profile", None),
                )
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
                    heading_path=canonical_heading_path(row.ModuleChunk.heading_path),
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
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
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
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(session, idempotency_key, idempotency_write)
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
                if not source.active:
                    raise ValueError("a scene from a retired module revision cannot become current")
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
            result = {
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
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

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
            "headings": list(canonical_heading_path(scene.headings)),
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
    def _actor_binding_view(row: ModuleActorBinding) -> dict[str, Any]:
        return {
            "id": row.id,
            "module_id": row.module_id,
            "scene_id": row.scene_id,
            "scene_key": row.scene_key or None,
            "character_id": row.character_id,
            "portable_actor_id": row.portable_actor_id,
            "binding_kind": row.binding_kind,
            "role": row.role,
            "metadata": dict(row.metadata_json or {}),
        }

    @staticmethod
    def _retired_source_key(session: Any, campaign_id: str, source_key: str, checksum: str) -> str:
        """Return a unique, human-auditable key for an immutable retired revision."""
        return unique_retired_source_key(
            source_key,
            checksum,
            exists=lambda candidate: bool(
                session.scalar(
                    select(ModuleSource.id).where(
                        ModuleSource.campaign_id == campaign_id,
                        ModuleSource.source_key == candidate,
                    )
                )
            ),
        )

    @staticmethod
    def _counts(session, module_id: str) -> tuple[int, int, int]:
        chapters = session.query(ModuleChapter).filter_by(module_id=module_id).count()
        scenes = session.query(ModuleScene).filter_by(module_id=module_id).count()
        chunks = session.query(ModuleChunk).filter_by(module_id=module_id).count()
        return chapters, scenes, chunks
