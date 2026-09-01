"""Document conversion contracts and layout-aware PDF normalization."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from statistics import median
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4
from weakref import WeakValueDictionary

DOCUMENT_NORMALIZER_VERSION = "37"
_MAX_STRUCTURAL_HEADING_CHARS = 200
DOCUMENT_SOURCE_SUFFIXES = frozenset({".md", ".markdown", ".pdf", ".txt"})
_DOCUMENT_CACHE_SCHEMA = 1
_PDF_EXTRACTION_CACHE_SCHEMA = 6
_PDF_TEXT_EXTRACTOR_VERSION = "11"
_OCR_PAGE_CACHE_SCHEMA = 1
_BOOKMARK_OCR_MAX_NON_WHITESPACE = 800
_RAPIDOCR_ENGINES: dict[str, tuple[Any, RLock]] = {}
_RAPIDOCR_ENGINES_LOCK = RLock()
_NORMALIZATION_CACHE_LOCKS: WeakValueDictionary[Path, RLock] = WeakValueDictionary()
_NORMALIZATION_CACHE_LOCKS_GUARD = RLock()


class DocumentQualityError(RuntimeError):
    """Raised when a source cannot provide enough content to parse safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DocumentLayoutProfile:
    """System-provided visual-layout exclusions for generic document conversion."""

    name: str = "generic"
    visual_heading_exclusion_patterns: tuple[str, ...] = ()
    repeated_margin_exclusion_patterns: tuple[str, ...] = ()

    def excludes_visual_heading(self, value: str) -> bool:
        return any(
            re.search(pattern, value) is not None
            for pattern in self.visual_heading_exclusion_patterns
        )

    def excludes_repeated_margin(self, value: str) -> bool:
        return any(
            re.search(pattern, value) is not None
            for pattern in self.repeated_margin_exclusion_patterns
        )

    @property
    def cache_identity(self) -> str:
        digest = hashlib.sha256(
            "\x1d".join(
                (
                    "\x1e".join(self.visual_heading_exclusion_patterns),
                    "\x1e".join(self.repeated_margin_exclusion_patterns),
                )
            ).encode("utf-8")
        ).hexdigest()[:12]
        return f"{self.name}:{digest}"


GENERIC_DOCUMENT_LAYOUT_PROFILE = DocumentLayoutProfile()


@dataclass(frozen=True)
class DocumentBookmark:
    title: str
    page: int
    depth: int


@dataclass(frozen=True)
class NormalizedDocument:
    content: str
    media_type: str
    source_path: str
    checksum: str
    page_count: int = 1
    bookmarks: tuple[DocumentBookmark, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


def _normalized_document_page_span(content: str, page_number: int) -> tuple[int, int]:
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    matches = list(_PAGE_MARKER_SCAN_RE.finditer(content))
    for index, match in enumerate(matches):
        if int(match.group(1)) != page_number:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        return match.end(), end
    raise ValueError(f"normalized document has no physical page {page_number}")


def normalized_document_page_text(
    document: NormalizedDocument,
    page_number: int,
) -> str:
    """Return the exact normalized text segment owned by one physical PDF page."""

    start, end = _normalized_document_page_span(document.content, page_number)
    return document.content[start:end]


def apply_document_page_revisions(
    document: NormalizedDocument,
    revisions: Sequence[Mapping[str, Any]] | None,
) -> NormalizedDocument:
    """Apply checksum-bound, exact-match Agent or human transcription repairs.

    The source document and cached OCR remain immutable.  A revision can only
    replace unique text inside one physical page segment, and the reviewed page
    must retain at least half of the original normalized text.  A wholly missed
    page can be recovered only by one empty-anchor replacement backed by the
    checksum of a rendered page reviewed by an Agent or human.
    """

    if not revisions:
        return document
    content = document.content
    audit: list[dict[str, Any]] = []
    reviewed_pages: set[int] = set()
    page_revision_counts: Counter[int] = Counter()
    for index, raw_revision in enumerate(revisions):
        if not isinstance(raw_revision, Mapping):
            raise ValueError(f"page revisions[{index}] must be an object")
        revision = dict(raw_revision)
        allowed = {
            "source_checksum",
            "page_number",
            "base_text_sha256",
            "replacements",
            "reviewer",
            "review_method",
            "rationale",
            "evidence",
        }
        unsupported = set(revision) - allowed
        if unsupported:
            raise ValueError(
                f"page revisions[{index}] has unsupported fields: {sorted(unsupported)}"
            )
        if str(revision.get("source_checksum") or "") != document.checksum:
            raise ValueError(f"page revisions[{index}] source checksum does not match")
        page_number = revision.get("page_number")
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or not 1 <= page_number <= document.page_count
        ):
            raise ValueError(f"page revisions[{index}].page_number is invalid")
        page_revision_counts[page_number] += 1
        if page_revision_counts[page_number] > 8:
            raise ValueError("at most eight transcription revisions are allowed per page")
        reviewed_pages.add(page_number)
        reviewer = str(revision.get("reviewer") or "").strip()
        if not 1 <= len(reviewer) <= 200:
            raise ValueError(f"page revisions[{index}].reviewer is required")
        review_method = str(revision.get("review_method") or "").strip()
        if review_method not in {"agent", "human"}:
            raise ValueError(f"page revisions[{index}].review_method must be agent or human")
        rationale = str(revision.get("rationale") or "").strip()
        if not 1 <= len(rationale) <= 2000:
            raise ValueError(f"page revisions[{index}].rationale is required")
        evidence = revision.get("evidence")
        if not isinstance(evidence, Mapping) or not evidence:
            raise ValueError(f"page revisions[{index}].evidence is required")

        current_document = NormalizedDocument(
            content=content,
            media_type=document.media_type,
            source_path=document.source_path,
            checksum=document.checksum,
            page_count=document.page_count,
            bookmarks=document.bookmarks,
            warnings=document.warnings,
            metadata=document.metadata,
        )
        page_start, page_end = _normalized_document_page_span(content, page_number)
        page_text = current_document.content[page_start:page_end]
        base_checksum = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
        if str(revision.get("base_text_sha256") or "") != base_checksum:
            raise ValueError(f"page revisions[{index}] base text checksum does not match")
        raw_replacements = revision.get("replacements")
        if not isinstance(raw_replacements, list) or not 1 <= len(raw_replacements) <= 128:
            raise ValueError(f"page revisions[{index}].replacements must contain 1 to 128 entries")
        revised_page = page_text
        replacements: list[dict[str, str]] = []
        empty_page_recovery = (
            not page_text.strip()
            and len(raw_replacements) == 1
            and isinstance(raw_replacements[0], Mapping)
            and str(dict(raw_replacements[0]).get("old", "")) == ""
        )
        if empty_page_recovery and str(dict(evidence).get("basis") or "") != "rendered_page":
            raise ValueError(
                f"page revisions[{index}] empty-page recovery requires rendered_page evidence"
            )
        for replacement_index, raw_replacement in enumerate(raw_replacements):
            if not isinstance(raw_replacement, Mapping):
                raise ValueError(
                    f"page revisions[{index}].replacements[{replacement_index}] must be an object"
                )
            replacement = dict(raw_replacement)
            if set(replacement) != {"old", "new"}:
                raise ValueError(
                    f"page revisions[{index}].replacements[{replacement_index}] "
                    "must contain only old and new"
                )
            old = str(replacement["old"])
            new = str(replacement["new"])
            maximum_new_length = 50000 if empty_page_recovery else 500
            if (
                (not old and not empty_page_recovery)
                or not new
                or old == new
                or len(old) > 500
                or len(new) > maximum_new_length
            ):
                raise ValueError(
                    f"page revisions[{index}].replacements[{replacement_index}] is invalid"
                )
            if re.search(r"<!--\s*page\s*:\s*\d+\s*-->", new, re.IGNORECASE):
                raise ValueError(
                    f"page revisions[{index}].replacements[{replacement_index}].new "
                    "cannot create a physical page marker"
                )
            if empty_page_recovery:
                revised_page = new
                replacements.append({"old": old, "new": new})
                continue
            if revised_page.count(old) != 1:
                raise ValueError(
                    f"page revisions[{index}].replacements[{replacement_index}].old "
                    "must match exactly once on the reviewed page"
                )
            revised_page = revised_page.replace(old, new, 1)
            replacements.append({"old": old, "new": new})
        original_size = len(re.sub(r"\s+", "", page_text))
        revised_size = len(re.sub(r"\s+", "", revised_page))
        if original_size >= 24 and not (
            revised_size >= max(24, int(original_size * 0.5))
            and revised_size <= max(24, int(original_size * 1.5))
        ):
            raise ValueError(
                f"page revisions[{index}] must preserve between 50% and 150% of page text"
            )
        content = content[:page_start] + revised_page + content[page_end:]
        audit.append(
            {
                "page_number": page_number,
                "base_text_sha256": base_checksum,
                "revised_text_sha256": hashlib.sha256(revised_page.encode("utf-8")).hexdigest(),
                "reviewer": reviewer,
                "review_method": review_method,
                "rationale": rationale,
                "evidence": dict(evidence),
                "replacements": replacements,
            }
        )
    revision_checksum = hashlib.sha256(
        json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return NormalizedDocument(
        content=content,
        media_type=document.media_type,
        source_path=document.source_path,
        checksum=document.checksum,
        page_count=document.page_count,
        bookmarks=document.bookmarks,
        warnings=document.warnings,
        metadata={
            **document.metadata,
            "text_revision_count": len(audit),
            "text_revision_pages": sorted(reviewed_pages),
            "text_revision_checksum": revision_checksum,
            "text_revisions": audit,
        },
    )


@dataclass(frozen=True)
class RenderedDocumentPage:
    """One visually faithful raster page with source provenance."""

    content: bytes
    media_type: str
    source_path: str
    source_checksum: str
    page_number: int
    page_count: int
    width: int
    height: int
    scale: float
    checksum: str


@dataclass(frozen=True)
class OcrTextBlock:
    """One checksum-independent OCR text block with page-space coordinates."""

    text: str
    confidence: float
    x0: float
    y0: float
    x1: float
    y1: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "bbox": [self.x0, self.y0, self.x1, self.y1],
        }


@dataclass(frozen=True)
class OcrPageLayout:
    """Text-only OCR evidence that preserves enough geometry to recover columns."""

    page_number: int
    width: int
    height: int
    blocks: tuple[OcrTextBlock, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "blocks": [block.as_dict() for block in self.blocks],
        }


class DocumentConverter(Protocol):
    def convert(
        self,
        path: str | Path,
        *,
        source_checksum: str | None = None,
    ) -> NormalizedDocument: ...


class OcrProvider(Protocol):
    name: str

    def extract(
        self,
        path: str | Path,
        *,
        page_numbers: Sequence[int] | None = None,
    ) -> list[str]: ...


_CHAPTER_RE = re.compile(
    r"^(?:(?:第[一二三四五六七八九十百0-9]+章|附录\s*[A-ZＡ-Ｚ])(?:\s|：|:)|"
    r"(?:Ch(?:apter)?\s*\.?|App(?:endix)?\s*\.?|Part|Episodes?)\s+"
    r"(?:[0-9A-Z]+(?:\s+and\s+[0-9A-Z]+)?)(?:\s|：|:|-))",
    re.IGNORECASE,
)
_ROOM_RE = re.compile(
    r"^(?:(?=[A-Z]{1,3}\s*[0-9IlO]{1,3}[A-Za-z]?\s*[.．])"
    r"(?=[^.．]*\d)"
    r"[A-Z]{1,3}\s*[0-9IlO]{1,3}[A-Za-z]?"
    r"|[A-Z]{1,3}\s*[Il][0-9IlO]{0,2})"
    r"\s*[.．]\s*(?=[^\W_])\S+",
    re.IGNORECASE,
)
_ROOM_CODE_RE = re.compile(
    r"^(?P<prefix>[A-Z]{1,3}?)\s*"
    r"(?P<number>[0-9IlO]{1,3})(?P<suffix>[A-Za-z]?)"
    r"(?P<separator>\s*[.．]\s*)(?P<title>[^\W_]\S.*)$",
    re.IGNORECASE,
)
_LIST_RE = re.compile(r"^(?:[-*•●▪◼]|\d+[.)、]|[A-Za-z][.)])\s*")
_PAGE_NUMBER_RE = re.compile(r"^\d{1,4}$")
_TERMINAL_RE = re.compile(r"[。！？!?；;：:…][”’』」）》】]*$")
_PAGE_MARKER_RE = re.compile(r"^<!-- page: \d+ -->$")
_PAGE_MARKER_SCAN_RE = re.compile(r"(?m)^<!-- page: (\d+) -->$")


def file_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading a complete rulebook into memory."""
    digest = hashlib.sha256()
    with Path(path).expanduser().resolve().open("rb") as stream:
        while block := stream.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


class PageLocator:
    """Resolve normalized-content offsets to pages in O(log n) time."""

    def __init__(self, content: str) -> None:
        markers = [
            (match.start(), int(match.group(1))) for match in _PAGE_MARKER_SCAN_RE.finditer(content)
        ]
        self._offsets = [item[0] for item in markers]
        self._pages = [item[1] for item in markers]

    def page_for_offset(self, offset: int) -> int | None:
        index = bisect_right(self._offsets, offset) - 1
        return self._pages[index] if index >= 0 else None


def _normalize(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold())


def _clean_line(value: str) -> str:
    value = value.replace("\uf06c", "•").replace("\uf0b7", "•")
    value = "".join(" " if 0xE000 <= ord(char) <= 0xF8FF else char for char in value)
    return re.sub(r"[ \t]+", " ", value).strip()


def _canonical_room_heading(value: str) -> str:
    """Repair unambiguous OCR substitutions inside a recognized room code."""
    matched = _ROOM_CODE_RE.match(value)
    if matched is None:
        return value
    number = matched.group("number").translate(
        str.maketrans({"I": "1", "i": "1", "l": "1", "O": "0", "o": "0"})
    )
    return (
        f"{matched.group('prefix').upper()}{number}{matched.group('suffix')}"
        f"{matched.group('separator')}{matched.group('title')}"
    )


def _looks_like_room_heading(value: str) -> bool:
    """Require a room-like label instead of treating wrapped prose as an OCR code."""
    if _looks_like_corrupt_visual_heading(value):
        return False
    matched = _ROOM_CODE_RE.match(value)
    if matched is None:
        return False
    prefix = matched.group("prefix").upper()
    suffix = matched.group("suffix")
    title = matched.group("title").strip()
    if prefix == "MAP":
        return False
    if prefix == "DAY" and not suffix:
        return True
    if (
        suffix
        and suffix.islower()
        and len(prefix) == 1
        and any(char in "23456789" for char in matched.group("number"))
    ):
        return True
    label, separator, _body = title.partition(". ")
    if not separator:
        label, separator, _body = title.partition(" --- ")
    candidate = label if separator else title
    words = re.findall(r"[A-Za-z]+", candidate)
    if not words:
        return False
    capitalized = sum(word[:1].isupper() for word in words)
    return bool(
        _looks_like_all_caps_heading(candidate)
        or _looks_like_letter_spaced_heading(candidate)
        or (
            len(words) <= 6
            and capitalized / len(words) >= 0.5
            and not candidate.endswith((",", ";", ":"))
        )
    )


def _split_room_heading(value: str) -> tuple[str, str]:
    """Separate an inline room label from prose extracted on the same line."""
    matched = _ROOM_CODE_RE.match(value)
    if matched is None:
        return value, ""
    canonical = _canonical_room_heading(value)
    canonical_match = _ROOM_CODE_RE.match(canonical)
    if canonical_match is None:
        return canonical, ""
    code = (
        f"{canonical_match.group('prefix').upper()}"
        f"{canonical_match.group('number')}"
        f"{canonical_match.group('suffix')}"
    )
    title = canonical_match.group("title").strip()
    label, separator, body = title.partition(". ")
    delimiter = "."
    if not separator:
        label, separator, body = title.partition(" --- ")
        delimiter = ""
    if separator and _looks_like_room_heading(f"{code}. {label}"):
        return f"{code}. {label}{delimiter}".rstrip("."), body.strip()
    words = re.findall(r"[A-Za-z]+", title)
    if (
        canonical_match.group("suffix").islower()
        or len(words) > 6
        or _prefix_is_day(canonical_match.group("prefix"))
    ):
        return code, title
    return canonical, ""


def _prefix_is_day(value: str) -> bool:
    """Keep day-number timeline labels while moving their prose into the body."""
    return value.casefold() == "day"


def _looks_letter_spaced(value: str) -> bool:
    """Recognize display-font extraction that splits words into letter tokens."""
    words = re.findall(r"[A-Za-z]+", value)
    singles = sum(len(word) == 1 for word in words)
    return singles >= 3 and singles / max(len(words), 1) >= 0.3


def _looks_like_letter_spaced_heading(value: str) -> bool:
    """Reject prose with a few short tokens while accepting damaged display text."""
    words = re.findall(r"[A-Za-z]+", value)
    singles = sum(len(word) == 1 for word in words)
    long_words = sum(len(word) > 3 for word in words)
    return bool(singles >= 3 and singles / max(len(words), 1) >= 0.55 and long_words <= 1)


def _bookmark_title(value: str) -> str:
    """Collapse control whitespace found in otherwise useful outline titles."""
    title = " ".join(value.split())
    return re.sub(r"^(Ch|App)\s+\.", r"\1.", title, flags=re.IGNORECASE)


def _prefer_bookmark_title(raw: str, bookmark: str, *, trusted: bool = False) -> bool:
    """Use an outline label when it repairs layout damage without losing text truth."""
    canonical = _bookmark_title(bookmark)
    if _looks_letter_spaced(raw):
        return True
    # Some outlines OCR the appendix letter B as the digit 8. The page heading
    # is stronger evidence in that narrow conflict.
    if re.match(r"^App\.?\s*\d", canonical, re.IGNORECASE) and re.match(
        r"^Appendix\s+[A-Z]", raw, re.IGNORECASE
    ):
        return False
    if trusted:
        return True
    raw_normalized = _normalize(raw)
    canonical_normalized = _normalize(canonical)
    return bool(
        raw_normalized
        and canonical_normalized
        and (
            (
                raw_normalized in canonical_normalized
                and len(canonical_normalized) >= len(raw_normalized) + 3
            )
            or (
                _CHAPTER_RE.match(canonical)
                and SequenceMatcher(None, raw_normalized, canonical_normalized).ratio() >= 0.88
            )
        )
    )


def _chapter_identity(value: str) -> str:
    """Compare abbreviated and full chapter labels as the same boundary."""
    text = value.strip()
    text = re.sub(r"^Ch(?:apter)?\s*\.?\s*", "chapter ", text, flags=re.IGNORECASE)
    text = re.sub(r"^App(?:endix)?\s*\.?\s*", "appendix ", text, flags=re.IGNORECASE)
    return _normalize(text)


def _chapter_label(value: str) -> str | None:
    identity = _chapter_identity(value)
    matched = re.match(r"^(chapter|appendix|part|episodes?)(\d+(?:and\d+)?|[a-z])", identity)
    if not matched:
        return None
    kind = "episode" if matched.group(1).startswith("episode") else matched.group(1)
    return f"{kind}:{matched.group(2)}"


def _looks_like_automatic_chapter_heading(value: str) -> bool:
    """Accept a chapter boundary without outline evidence only when unambiguous."""
    text = value.strip()
    if not _CHAPTER_RE.match(text):
        return False
    if re.search(r"\.{3,}\s*\d+\s*$", text) or re.search(r"\s\d+\s*$", text):
        return False
    if re.match(r"^(?:第[一二三四五六七八九十百0-9]+章|附录\s*[A-ZＡ-Ｚ])", text):
        return True
    # Parenthesized chapter references, prose such as "chapter 3 for an
    # example", and running headers corrupted to "CHAPTER 3 I TITLE" must not
    # become document boundaries merely because their font differs from body.
    return bool(
        re.match(
            r"^(?:Chapter|Appendix|Part|Episode)\s+[0-9A-Z]+\s*[:：—-]\s*\S+",
            text,
            re.IGNORECASE,
        )
    )


def _repeated_margin_lines(
    pages: list[list[str]],
    layout_profile: DocumentLayoutProfile,
) -> set[str]:
    candidates: Counter[str] = Counter()
    for lines in pages:
        nonempty = [line for line in lines if line]
        seen_on_page: set[str] = set()
        for line in [*nonempty[:3], *nonempty[-3:]]:
            if _CHAPTER_RE.match(line) or layout_profile.excludes_repeated_margin(line):
                continue
            normalized = _normalize(line)
            if (
                normalized
                and normalized not in seen_on_page
                and not _PAGE_NUMBER_RE.fullmatch(line)
            ):
                candidates[normalized] += 1
                seen_on_page.add(normalized)
    threshold = max(2, len(pages) // 8)
    return {line for line, count in candidates.items() if count >= threshold}


def _match_bookmarks(
    pages: list[list[str]],
    bookmarks: list[DocumentBookmark],
) -> tuple[
    dict[tuple[int, int], int],
    int,
    set[tuple[int, int]],
    dict[tuple[int, int], str],
    dict[int, list[str]],
]:
    levels: dict[tuple[int, int], int] = {}
    trusted_chapters: set[tuple[int, int]] = set()
    canonical_titles: dict[tuple[int, int], str] = {}
    synthetic_chapters: dict[int, list[str]] = {}
    structural_depths = [
        bookmark.depth for bookmark in bookmarks if _CHAPTER_RE.match(bookmark.title.strip())
    ]
    structural_depth = min(structural_depths) if structural_depths else None
    matched = 0
    for bookmark in bookmarks:
        if not 1 <= bookmark.page <= len(pages):
            continue
        if len(_bookmark_title(bookmark.title)) > _MAX_STRUCTURAL_HEADING_CHARS:
            continue
        target = _normalize(bookmark.title)
        structural_bookmark = bool(_CHAPTER_RE.match(bookmark.title.strip()))
        best_index = -1
        best_score = 0.0
        for index, line in enumerate(pages[bookmark.page - 1]):
            candidate = _normalize(line)
            if not target or not candidate:
                continue
            if (
                structural_bookmark
                and not _CHAPTER_RE.match(line)
                and not _looks_letter_spaced(line)
                and SequenceMatcher(None, target, candidate).ratio() < 0.68
            ):
                continue
            if target in candidate or candidate in target:
                score = min(len(target), len(candidate)) / max(len(target), len(candidate))
            else:
                score = SequenceMatcher(None, target, candidate).ratio()
            if score > best_score:
                best_score, best_index = score, index
        threshold = 0.45 if structural_bookmark else 0.68
        if best_index >= 0 and best_score >= threshold:
            key = (bookmark.page, best_index)
            level = min(4, 2 + bookmark.depth)
            levels[key] = min(level, levels.get(key, level))
            trusted_chapter = bool(
                structural_depth is not None
                and bookmark.depth == structural_depth
                and _CHAPTER_RE.match(bookmark.title.strip())
            )
            nonempty_before = sum(bool(line) for line in pages[bookmark.page - 1][:best_index])
            if trusted_chapter and nonempty_before > 8:
                title = _bookmark_title(bookmark.title)
                page_titles = synthetic_chapters.setdefault(bookmark.page, [])
                if _chapter_identity(title) not in {
                    _chapter_identity(item) for item in page_titles
                }:
                    page_titles.append(title)
                matched += 1
                continue
            if trusted_chapter:
                trusted_chapters.add(key)
            raw_title = pages[bookmark.page - 1][best_index]
            if _prefer_bookmark_title(raw_title, bookmark.title, trusted=trusted_chapter):
                canonical_titles[key] = _bookmark_title(bookmark.title)
            matched += 1
        elif (
            structural_depth is not None
            and bookmark.depth == structural_depth
            and structural_bookmark
        ):
            title = _bookmark_title(bookmark.title)
            page_titles = synthetic_chapters.setdefault(bookmark.page, [])
            if _chapter_identity(title) not in {_chapter_identity(item) for item in page_titles}:
                page_titles.append(title)
    return levels, matched, trusted_chapters, canonical_titles, synthetic_chapters


def _match_visual_headings(
    pages: list[list[str]],
    visual_headings: dict[int, list[tuple[str, int]]],
) -> tuple[dict[tuple[int, int], int], int]:
    levels: dict[tuple[int, int], int] = {}
    matched = 0
    for page_number, hints in visual_headings.items():
        if not 1 <= page_number <= len(pages):
            continue
        available = set(range(len(pages[page_number - 1])))
        for title, level in hints:
            target = _normalize(title)
            index = next(
                (
                    candidate
                    for candidate in sorted(available)
                    if target and _normalize(pages[page_number - 1][candidate]) == target
                ),
                None,
            )
            if index is None:
                continue
            available.remove(index)
            levels[(page_number, index)] = level
            matched += 1
    return levels, matched


def _joiner(left: str, right: str) -> str:
    if not left or not right:
        return ""
    if left.endswith("-") and right[:1].isascii() and right[:1].isalpha():
        return ""
    if "\u4e00" <= left[-1] <= "\u9fff" and "\u4e00" <= right[0] <= "\u9fff":
        return ""
    return " "


def _looks_like_all_caps_heading(value: str) -> bool:
    """Recover short visual subheadings that are absent from a PDF outline."""
    text = value.strip()
    # ``str.upper`` is a no-op for CJK characters.  Treating every uncased
    # alphabet as uppercase turns practically every short Chinese body line
    # into a heading.  This heuristic is deliberately limited to scripts
    # which actually carry case; CJK structure must come from the PDF outline
    # or another explicit structural signal.
    letters = [char for char in text if char.isascii() and char.isalpha()]
    uncased_letters = [char for char in text if char.isalpha() and not char.isascii()]
    uppercase_ratio = sum(char.isupper() for char in letters) / max(len(letters), 1)
    return bool(
        3 <= len(text) <= 80
        and 1 <= len(text.split()) <= 12
        and letters
        and not uncased_letters
        and (uppercase_ratio >= 0.85 or _looks_like_letter_spaced_heading(text))
        and not _TERMINAL_RE.search(text)
    )


def _recover_letter_spaced_heading(value: str, page_lines: list[str]) -> str:
    """Reuse a normally spaced mention of a damaged display heading when available."""
    if not _looks_letter_spaced(value):
        return value
    target = _normalize(value)
    if not target:
        return value
    candidates: list[str] = []
    for line in page_lines:
        if line == value:
            continue
        words = re.findall(r"[A-Za-z]+(?:['\u2019][A-Za-z]+)?", line)
        for start in range(len(words)):
            normalized = ""
            for end in range(start, min(len(words), start + 12)):
                normalized += _normalize(words[end])
                if len(normalized) > len(target):
                    break
                if normalized != target:
                    continue
                candidate = " ".join(words[start : end + 1])
                if not _looks_letter_spaced(candidate):
                    candidates.append(candidate)
    if not candidates:
        return value
    return min(candidates, key=lambda candidate: (len(candidate.split()), len(candidate)))


def _strip_decorative_heading_leader(value: str) -> str:
    """Remove repeated layout leaders without changing ordinary punctuation."""
    return re.sub(r"\s*(?:[-_=~]\s*){3,}$", "", value).strip()


def _looks_like_toc_page(lines: list[str]) -> bool:
    """Identify dense contents pages so their entries do not become body headings."""
    nonempty = [line for line in lines if line]
    if not nonempty:
        return False
    heading = " ".join(nonempty[:5]).casefold()
    compact_heading = _normalize(heading)
    named_contents = (
        "目录" in heading
        or bool(re.search(r"\bcontents\b", heading))
        or "tableofcontents" in compact_heading
    )
    chapter_entries = sum(bool(_CHAPTER_RE.match(line)) for line in nonempty)
    leader_entries = sum(bool(re.search(r"\.{3,}\s*\d+\s*$", line)) for line in nonempty)
    short_entries = sum(len(line) <= 80 for line in nonempty)
    return bool(
        named_contents
        and (chapter_entries >= 2 or leader_entries >= 5)
        and len(nonempty) >= 8
        and (leader_entries >= 5 or short_entries / len(nonempty) >= 0.75)
    )


def _reflow_page(
    page_number: int,
    lines: list[str],
    heading_levels: dict[tuple[int, int], int],
    repeated_margins: set[str],
    *,
    structural_headings: bool = True,
    trusted_chapters: set[tuple[int, int]] | None = None,
    trusted_chapter_titles: set[str] | None = None,
    canonical_titles: dict[tuple[int, int], str] | None = None,
    synthetic_chapters: list[str] | None = None,
) -> tuple[list[str], int, int]:
    output = [f"<!-- page: {page_number} -->", ""]
    paragraph: list[str] = []
    synthetic_chapters = synthetic_chapters or []
    for title in synthetic_chapters:
        output.extend((f"# {title}", ""))
    heading_count = len(synthetic_chapters)
    room_count = 0
    last_heading_identity = _normalize(synthetic_chapters[-1]) if synthetic_chapters else ""
    body_since_heading = False

    def flush() -> None:
        nonlocal body_since_heading
        if not paragraph:
            return
        merged = paragraph[0]
        for line in paragraph[1:]:
            if merged.endswith("-") and line[:1].isascii() and line[:1].isalpha():
                merged = merged[:-1] + line
            else:
                merged += _joiner(merged, line) + line
        if re.match(r"^#{1,6}\s", merged):
            # Extracted prose can begin with a literal hash glyph (for example,
            # a damaged alphabet table immediately before body text). Only this
            # renderer may create Markdown structure, so quote source glyphs
            # that would otherwise inject an unbounded section heading.
            merged = f"\\{merged}"
        output.extend((merged, ""))
        paragraph.clear()
        body_since_heading = True

    nonempty = [index for index, line in enumerate(lines) if line]
    margins = set(nonempty[:3] + nonempty[-3:])
    top_lines = set(nonempty[:5])
    chapter_lines = sum(bool(_CHAPTER_RE.match(line)) for line in lines if line)
    trusted_chapters = trusted_chapters or set()
    trusted_chapter_titles = trusted_chapter_titles or set()
    canonical_titles = canonical_titles or {}
    for index, line in enumerate(lines):
        if not line:
            flush()
            continue
        if index in margins and _normalize(line) in repeated_margins:
            continue
        if index in margins and _PAGE_NUMBER_RE.fullmatch(line):
            continue
        key = (page_number, index)
        display_line = _strip_decorative_heading_leader(canonical_titles.get(key, line))
        room_body = ""
        level = heading_levels.get(key) if structural_headings else None
        next_line = next((value for value in lines[index + 1 :] if value), "")
        previous_line = next((value for value in reversed(lines[:index]) if value), "")
        if (
            structural_headings
            and re.match(r"^Chapter\s+[0-9A-Z]", line, re.IGNORECASE)
            and re.match(r"^第[一二三四五六七八九十百0-9]+章", previous_line)
        ):
            # A bilingual chapter title is frequently extracted as two adjacent
            # lines.  The Chinese title already established the boundary.
            continue
        chapter_confirmation = bool(
            re.match(r"^(?:Chapter|Appendix)\s+[0-9A-Z]", next_line, re.IGNORECASE)
        )
        trusted_top_level = key in trusted_chapters
        identity = _chapter_identity(display_line)
        duplicate_trusted_chapter = any(
            identity == trusted
            or (
                len(trusted) >= 12 and len(identity) > len(trusted) and identity.startswith(trusted)
            )
            or (
                len(identity) >= 12
                and len(trusted) >= 12
                and _chapter_label(identity) == _chapter_label(trusted)
                and _chapter_label(identity) is not None
                and SequenceMatcher(None, identity, trusted).ratio() >= 0.88
            )
            for trusted in trusted_chapter_titles
        )
        if (
            level is not None
            and not trusted_top_level
            and _looks_like_corrupt_visual_heading(display_line)
        ):
            level = None
        if level is not None and len(display_line) > _MAX_STRUCTURAL_HEADING_CHARS:
            # Broken PDF outlines sometimes contain an entire body paragraph and
            # point it back at that same paragraph. Such a match is not document
            # structure and must remain searchable prose.
            level = None
        if not trusted_top_level and _CHAPTER_RE.match(display_line) and duplicate_trusted_chapter:
            # Drop duplicated running headers or visual recovery of a boundary
            # already anchored by an outline entry elsewhere in the document.
            continue
        if (
            structural_headings
            and _CHAPTER_RE.match(display_line)
            and (
                trusted_top_level
                or (
                    _looks_like_automatic_chapter_heading(display_line)
                    and (
                        level is not None
                        or chapter_confirmation
                        or (index in top_lines and chapter_lines == 1)
                    )
                )
            )
        ):
            level = 1
        elif structural_headings and _looks_like_room_heading(display_line):
            display_line, room_body = _split_room_heading(display_line)
            level = level or 4
            room_count += 1
        elif (
            structural_headings
            and level is None
            and not _looks_like_corrupt_visual_heading(display_line)
            and _looks_like_all_caps_heading(display_line)
        ):
            level = 5
        if level is not None:
            display_line = _recover_letter_spaced_heading(display_line, lines)
            flush()
            identity = _normalize(display_line)
            if (
                identity
                and last_heading_identity
                and not body_since_heading
                and len(identity) < len(last_heading_identity)
                and last_heading_identity.endswith(identity)
            ):
                if room_body:
                    paragraph.append(room_body)
                    if _TERMINAL_RE.search(room_body):
                        flush()
                continue
            output.extend((f"{'#' * level} {display_line}", ""))
            heading_count += 1
            last_heading_identity = identity
            body_since_heading = False
            if room_body:
                paragraph.append(room_body)
                if _TERMINAL_RE.search(room_body):
                    flush()
        elif _LIST_RE.match(line):
            flush()
            output.append(re.sub(r"^[•●▪◼]\s*", "- ", line))
            body_since_heading = True
        else:
            paragraph.append(line)
            if _TERMINAL_RE.search(line):
                flush()
    flush()
    return output, heading_count, room_count


def build_structured_markdown(
    page_texts: list[str],
    bookmarks: list[DocumentBookmark] | None = None,
    visual_headings: dict[int, list[tuple[str, int]]] | None = None,
    layout_profile: DocumentLayoutProfile = GENERIC_DOCUMENT_LAYOUT_PROFILE,
) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    """Normalize extracted PDF pages into provenance-preserving Markdown."""
    bookmarks = bookmarks or []
    pages = [[_clean_line(line) for line in text.splitlines()] for text in page_texts]
    matchable_bookmarks = [
        bookmark
        for bookmark in bookmarks
        if 1 <= bookmark.page <= len(pages)
        and len(_bookmark_title(bookmark.title)) <= _MAX_STRUCTURAL_HEADING_CHARS
        and any(_normalize(line) for line in pages[bookmark.page - 1])
    ]
    repeated = _repeated_margin_lines(pages, layout_profile)
    (
        heading_levels,
        matched,
        trusted_chapters,
        canonical_titles,
        synthetic_chapters,
    ) = _match_bookmarks(pages, matchable_bookmarks)
    trusted_chapter_titles = {
        _chapter_identity(canonical_titles.get(key, pages[key[0] - 1][key[1]]))
        for key in trusted_chapters
    }
    trusted_chapter_titles.update(
        _chapter_identity(title) for titles in synthetic_chapters.values() for title in titles
    )
    visual_levels, matched_visual = _match_visual_headings(pages, visual_headings or {})
    for key, level in visual_levels.items():
        heading_levels.setdefault(key, level)
    toc_pages = {
        page_number
        for page_number, lines in enumerate(pages, start=1)
        if _looks_like_toc_page(lines)
    }
    output: list[str] = []
    heading_count = room_count = 0
    for page_number, lines in enumerate(pages, start=1):
        rendered, headings, rooms = _reflow_page(
            page_number,
            lines,
            heading_levels,
            repeated,
            structural_headings=page_number not in toc_pages,
            trusted_chapters=trusted_chapters,
            trusted_chapter_titles=trusted_chapter_titles,
            canonical_titles=canonical_titles,
            synthetic_chapters=synthetic_chapters.get(page_number),
        )
        output.extend(rendered)
        heading_count += headings
        room_count += rooms
    warnings: list[str] = []
    if matchable_bookmarks and matched / len(matchable_bookmarks) < 0.95:
        warnings.append(
            "text-bearing bookmark match rate is "
            f"{matched}/{len(matchable_bookmarks)}; expected at least 95%"
        )
    if heading_count == 0:
        warnings.append("no structural headings were recovered")
    content = "\n".join(output).strip() + "\n"
    return (
        content,
        {
            "bookmark_count": len(bookmarks),
            "matchable_bookmark_count": len(matchable_bookmarks),
            "matched_bookmarks": matched,
            "synthetic_outline_headings": sum(map(len, synthetic_chapters.values())),
            "visual_heading_count": len(visual_levels),
            "matched_visual_headings": matched_visual,
            "heading_count": heading_count,
            "room_heading_count": room_count,
            "toc_pages": sorted(toc_pages),
        },
        tuple(warnings),
    )


def _page_quality(text: str) -> dict[str, Any]:
    characters = len(text)
    non_whitespace = sum(not char.isspace() for char in text)
    alphabetic = sum(char.isalpha() for char in text)
    whitespace = sum(char.isspace() for char in text)
    alphabetic_runs = re.findall(r"[^\W\d_]+", text, flags=re.UNICODE)
    long_alphabetic_runs = sum(len(run) >= 30 for run in alphabetic_runs)
    # Some PDFs expose a superficially complete text layer while omitting nearly
    # every inter-word space.  Character/control ratios cannot distinguish that
    # output from prose, but it is unusable for retrieval and heading parsing.
    # Require several implausibly long word runs as well as a very low separator
    # rate so URLs, identifiers, tables, and ordinary German compounds do not
    # force OCR by themselves.
    whitespace_per_alpha = whitespace / max(alphabetic, 1)
    fused_text = alphabetic >= 200 and whitespace_per_alpha < 0.08 and long_alphabetic_runs >= 3
    private_use = sum(unicodedata.category(char) == "Co" for char in text)
    control = sum(unicodedata.category(char) == "Cc" and char not in "\t\r\n" for char in text)
    replacement = text.count("\ufffd")
    ascii_words = re.findall(r"[A-Za-z]+", text)
    isolated_fragments = re.findall(
        r"(?<![A-Za-z])([B-HJ-Zb-hj-z])(?![A-Za-z])",
        text,
    )
    damaged_line_starts = sum(
        bool(re.match(r"^\s*(?:[-~\\:;_.?]|[0-9](?=[A-Za-z]))", line))
        for line in text.splitlines()
        if line.strip()
    )
    # Broken PDF font maps often yield entirely valid ASCII while dropping the
    # first glyph of words (``-he``, ``~od``) and emitting the missing glyph as
    # an isolated token. Neither Unicode corruption nor character-count gates
    # see that damage. Require both independent symptoms at prose scale so
    # bullets, stat rows, dice notation, abbreviations, and tables stay valid.
    lexical_damage = (
        len(ascii_words) >= 200
        and damaged_line_starts >= 4
        and len(isolated_fragments) >= 8
        and len(isolated_fragments) / len(ascii_words) >= 0.015
    )
    denominator = max(characters, 1)
    return {
        "characters": characters,
        "non_whitespace_characters": non_whitespace,
        "alphabetic_characters": alphabetic,
        "whitespace_per_alphabetic_character": whitespace_per_alpha,
        "long_alphabetic_run_count": long_alphabetic_runs,
        "private_use_characters": private_use,
        "control_characters": control,
        "replacement_characters": replacement,
        "isolated_ascii_fragment_count": len(isolated_fragments),
        "damaged_ascii_line_start_count": damaged_line_starts,
        "private_use_ratio": private_use / denominator,
        "control_ratio": control / denominator,
        "replacement_ratio": replacement / denominator,
        "sparse": non_whitespace < 20,
        "fused_text": fused_text,
        "lexically_damaged": lexical_damage,
        "corrupt": (
            private_use / denominator >= 0.02
            or control / denominator >= 0.01
            or replacement / denominator >= 0.01
        ),
    }


def _document_quality(page_texts: Sequence[str]) -> dict[str, Any]:
    page_stats = [_page_quality(text) for text in page_texts]
    characters = sum(int(item["characters"]) for item in page_stats)
    non_whitespace = sum(int(item["non_whitespace_characters"]) for item in page_stats)
    private_use = sum(int(item["private_use_characters"]) for item in page_stats)
    control = sum(int(item["control_characters"]) for item in page_stats)
    replacement = sum(int(item["replacement_characters"]) for item in page_stats)
    denominator = max(characters, 1)
    sparse_pages = [index for index, item in enumerate(page_stats, start=1) if item["sparse"]]
    corrupt_pages = [index for index, item in enumerate(page_stats, start=1) if item["corrupt"]]
    fused_pages = [index for index, item in enumerate(page_stats, start=1) if item["fused_text"]]
    lexical_damage_pages = [
        index for index, item in enumerate(page_stats, start=1) if item["lexically_damaged"]
    ]
    suspect_pages = sorted(
        set(sparse_pages) | set(corrupt_pages) | set(fused_pages) | set(lexical_damage_pages)
    )
    return {
        "character_count": characters,
        "non_whitespace_character_count": non_whitespace,
        "text_page_count": len(page_stats) - len(sparse_pages),
        "sparse_page_count": len(sparse_pages),
        "sparse_pages": sparse_pages,
        "corrupt_text_page_count": len(corrupt_pages),
        "corrupt_text_pages": corrupt_pages,
        "fused_text_page_count": len(fused_pages),
        "fused_text_pages": fused_pages,
        "lexical_damage_page_count": len(lexical_damage_pages),
        "lexical_damage_pages": lexical_damage_pages,
        "suspect_page_count": len(suspect_pages),
        "suspect_pages": suspect_pages,
        "private_use_character_count": private_use,
        "private_use_ratio": round(private_use / denominator, 6),
        "control_character_count": control,
        "control_ratio": round(control / denominator, 6),
        "replacement_character_count": replacement,
        "replacement_ratio": round(replacement / denominator, 6),
        "text_page_coverage": round(
            (len(page_stats) - len(sparse_pages)) / max(len(page_stats), 1), 6
        ),
    }


def _repair_pdf_word_break_noncharacters(
    page_texts: Sequence[str],
) -> tuple[list[str], int]:
    """Remove pypdfium2's U+FFFE discretionary word-break sentinel."""

    count = sum(str(page).count("\ufffe") for page in page_texts)
    return [re.sub(r"\ufffe[ \t\r\n]*", "", str(page)) for page in page_texts], count


def _repair_pdf_control_artifacts(page_texts: Sequence[str]) -> tuple[list[str], int]:
    """Remove the U+0002 glyph-position marker emitted by damaged PDF font maps."""

    count = sum(str(page).count("\x02") for page in page_texts)
    return [str(page).replace("\x02", "") for page in page_texts], count


def _ocr_page_cache_path(
    cache_dir: Path,
    *,
    source_checksum: str,
    profile: str,
    page_number: int,
) -> Path:
    profile_hash = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:16]
    return (
        cache_dir
        / "ocr-pages"
        / source_checksum[:2]
        / source_checksum
        / profile_hash
        / f"page-{page_number:05d}.json"
    )


def _ocr_layout_checksum(layout: dict[str, Any]) -> str:
    encoded = json.dumps(
        layout,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_ocr_page_cache(
    cache_dir: Path,
    *,
    source_checksum: str,
    profile: str,
    layout: OcrPageLayout,
) -> None:
    layout_value = layout.as_dict()
    _write_json_atomic(
        _ocr_page_cache_path(
            cache_dir,
            source_checksum=source_checksum,
            profile=profile,
            page_number=layout.page_number,
        ),
        {
            "schema": _OCR_PAGE_CACHE_SCHEMA,
            "source_checksum": source_checksum,
            "profile": profile,
            "page_number": layout.page_number,
            "layout_checksum": _ocr_layout_checksum(layout_value),
            "layout": layout_value,
        },
    )


def _read_ocr_page_cache(
    cache_dir: Path,
    *,
    source_checksum: str,
    profile: str,
    page_number: int,
) -> OcrPageLayout | None:
    target = _ocr_page_cache_path(
        cache_dir,
        source_checksum=source_checksum,
        profile=profile,
        page_number=page_number,
    )
    if not target.is_file():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        if (
            set(value)
            != {
                "schema",
                "source_checksum",
                "profile",
                "page_number",
                "layout_checksum",
                "layout",
            }
            or value.get("schema") != _OCR_PAGE_CACHE_SCHEMA
            or value.get("source_checksum") != source_checksum
            or value.get("profile") != profile
            or value.get("page_number") != page_number
        ):
            return None
        layout = value.get("layout")
        if not isinstance(layout, dict) or set(layout) != {
            "page_number",
            "width",
            "height",
            "blocks",
        }:
            return None
        if value.get("layout_checksum") != _ocr_layout_checksum(layout):
            return None
        width = layout.get("width")
        height = layout.get("height")
        blocks = layout.get("blocks")
        if (
            layout.get("page_number") != page_number
            or isinstance(width, bool)
            or not isinstance(width, int)
            or width < 1
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height < 1
            or not isinstance(blocks, list)
        ):
            return None
        parsed_blocks: list[OcrTextBlock] = []
        for block in blocks:
            if not isinstance(block, dict) or set(block) != {
                "text",
                "confidence",
                "bbox",
            }:
                return None
            text = block.get("text")
            confidence = block.get("confidence")
            bbox = block.get("bbox")
            if (
                not isinstance(text, str)
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
                or not 0 <= float(confidence) <= 1
                or not isinstance(bbox, list)
                or len(bbox) != 4
                or any(
                    isinstance(coordinate, bool)
                    or not isinstance(coordinate, (int, float))
                    or not math.isfinite(float(coordinate))
                    for coordinate in bbox
                )
            ):
                return None
            parsed_blocks.append(
                OcrTextBlock(
                    text=text,
                    confidence=float(confidence),
                    x0=float(bbox[0]),
                    y0=float(bbox[1]),
                    x1=float(bbox[2]),
                    y1=float(bbox[3]),
                )
            )
        return OcrPageLayout(
            page_number=page_number,
            width=width,
            height=height,
            blocks=tuple(parsed_blocks),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _new_rapidocr_engine(model_name: str) -> Any:
    try:
        from rapidocr import (
            EngineType,
            LangDet,
            LangRec,
            ModelType,
            OCRVersion,
            RapidOCR,
        )
    except ImportError as exc:
        raise RuntimeError("OCR requires `pip install sagasmith-core[documents,ocr]`") from exc
    model_type = ModelType.MEDIUM if model_name == "medium" else ModelType.SMALL
    return RapidOCR(
        params={
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.lang_type": LangDet.EN,
            "Det.model_type": model_type,
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.lang_type": LangRec.EN,
            "Rec.model_type": model_type,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
        }
    )


class RapidOcrProvider:
    """Lazy local OCR for pages whose PDF text layer is empty or corrupt."""

    name = "rapidocr"

    def __init__(
        self,
        *,
        scale: float = 2.0,
        model_type: str = "medium",
        cache_dir: str | Path | None = None,
    ) -> None:
        if not 1.0 <= scale <= 4.0:
            raise ValueError("OCR scale must be between 1.0 and 4.0")
        if model_type not in {"small", "medium"}:
            raise ValueError("OCR model_type must be small or medium")
        self.scale = float(scale)
        self.model_type = model_type
        self.cache_dir = Path(cache_dir).expanduser().resolve() if cache_dir is not None else None
        self.cache_hits = 0
        self.cache_misses = 0
        self._engine: Any | None = None
        self._engine_lock: RLock | None = None

    @property
    def cache_profile(self) -> str:
        try:
            rapidocr_version = package_version("rapidocr")
        except PackageNotFoundError:
            rapidocr_version = "unavailable"
        return (
            f"{self.name}:package={rapidocr_version}:ocr=PP-OCRv6:"
            f"model={self.model_type}:scale={self.scale:.2f}"
        )

    def _load_engine(self) -> Any:
        if self._engine is None:
            with _RAPIDOCR_ENGINES_LOCK:
                cached = _RAPIDOCR_ENGINES.get(self.model_type)
                if cached is None:
                    cached = (_new_rapidocr_engine(self.model_type), RLock())
                    _RAPIDOCR_ENGINES[self.model_type] = cached
                self._engine, self._engine_lock = cached
        return self._engine

    def extract(
        self,
        path: str | Path,
        *,
        page_numbers: Sequence[int] | None = None,
    ) -> list[str]:
        return [
            ocr_layout_text(layout)[0]
            for layout in self.extract_layout(path, page_numbers=page_numbers)
        ]

    def extract_layout(
        self,
        path: str | Path,
        *,
        page_numbers: Sequence[int] | None = None,
    ) -> list[OcrPageLayout]:
        """OCR selected pages while retaining block coordinates for text-only recovery.

        When a cache directory is configured, every page layout is stored under
        the immutable source checksum and exact OCR model profile.  This cache is
        intentionally independent from Markdown normalization versions: changing
        heading or chunking logic must not force an expensive image model to run
        again for the same source bytes.
        """

        source = Path(path).expanduser().resolve()
        selected = list(page_numbers) if page_numbers is not None else None
        if selected is None:
            source_checksum = file_sha256(source) if self.cache_dir is not None else ""

            def persist_all(layout: OcrPageLayout) -> None:
                if self.cache_dir is not None:
                    _write_ocr_page_cache(
                        self.cache_dir,
                        source_checksum=source_checksum,
                        profile=self.cache_profile,
                        layout=layout,
                    )
                    self.cache_misses += 1

            return self._extract_layout_uncached(
                source,
                page_numbers=None,
                on_page=persist_all,
            )

        if not selected:
            return []
        if any(
            isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1
            for page_number in selected
        ):
            raise ValueError("OCR page numbers must be positive integers")
        if self.cache_dir is None:
            return self._extract_layout_uncached(source, page_numbers=selected)

        source_checksum = file_sha256(source)
        profile = self.cache_profile
        layouts_by_page: dict[int, OcrPageLayout] = {}
        missing: list[int] = []
        for page_number in dict.fromkeys(selected):
            cached = _read_ocr_page_cache(
                self.cache_dir,
                source_checksum=source_checksum,
                profile=profile,
                page_number=page_number,
            )
            if cached is None:
                missing.append(page_number)
                self.cache_misses += 1
            else:
                layouts_by_page[page_number] = cached
                self.cache_hits += 1
        if missing:

            def persist_recovered(layout: OcrPageLayout) -> None:
                layouts_by_page[layout.page_number] = layout
                _write_ocr_page_cache(
                    self.cache_dir,
                    source_checksum=source_checksum,
                    profile=profile,
                    layout=layout,
                )

            recovered = self._extract_layout_uncached(
                source,
                page_numbers=missing,
                on_page=persist_recovered,
            )
            if len(recovered) != len(missing):
                raise DocumentQualityError(
                    "pdf_ocr_page_mismatch",
                    "OCR provider returned a different number of pages than requested",
                )
            for expected_page, layout in zip(missing, recovered, strict=True):
                if layout.page_number != expected_page:
                    raise DocumentQualityError(
                        "pdf_ocr_page_mismatch",
                        "OCR provider returned a layout for the wrong page",
                    )
                layouts_by_page[expected_page] = layout
        return [layouts_by_page[page_number] for page_number in selected]

    def _extract_layout_uncached(
        self,
        source: Path,
        *,
        page_numbers: Sequence[int] | None,
        on_page: Callable[[OcrPageLayout], None] | None = None,
    ) -> list[OcrPageLayout]:
        """Run the configured image model without consulting the page cache."""

        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise RuntimeError("OCR requires `pip install sagasmith-core[documents,ocr]`") from exc
        engine = self._load_engine()
        document = pdfium.PdfDocument(str(source))
        try:
            selected = list(range(1, len(document) + 1) if page_numbers is None else page_numbers)
            if any(not 1 <= page_number <= len(document) for page_number in selected):
                raise ValueError("OCR page number is outside the PDF")
            pages: list[OcrPageLayout] = []
            for page_number in selected:
                page = document[page_number - 1]
                try:
                    bitmap = page.render(scale=self.scale)
                    try:
                        image = bitmap.to_numpy()
                        assert self._engine_lock is not None
                        with self._engine_lock:
                            output = engine(image)
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                layout = _ocr_page_layout(
                    output,
                    page_number=page_number,
                    image_shape=image.shape,
                )
                pages.append(layout)
                if on_page is not None:
                    on_page(layout)
            return pages
        finally:
            document.close()


class CascadingOcrProvider:
    """Use stronger local OCR only for pages the preferred model cannot recover."""

    name = "rapidocr-cascade"

    def __init__(
        self,
        *providers: OcrProvider,
        minimum_layout_confidence: float = 0.86,
    ) -> None:
        if len(providers) < 2:
            raise ValueError("OCR cascade requires at least two providers")
        if not 0 <= minimum_layout_confidence <= 1:
            raise ValueError("minimum_layout_confidence must be between 0 and 1")
        self.providers = tuple(providers)
        self.minimum_layout_confidence = float(minimum_layout_confidence)

    @property
    def cache_profile(self) -> str:
        profiles = [
            str(
                getattr(provider, "cache_profile", None)
                or getattr(provider, "name", type(provider).__name__)
            )
            for provider in self.providers
        ]
        return f"{self.name}:min-confidence={self.minimum_layout_confidence:.3f}:" + "=>".join(
            profiles
        )

    def extract(
        self,
        path: str | Path,
        *,
        page_numbers: Sequence[int] | None = None,
    ) -> list[str]:
        requested = list(page_numbers) if page_numbers is not None else None
        selected: list[str] | None = None
        selected_pages: list[int] | None = requested
        pending_indexes: list[int] = []
        errors: list[Exception] = []
        for provider in self.providers:
            provider_pages = (
                None
                if selected is None and requested is None
                else [(selected_pages or [])[index] for index in pending_indexes]
                if selected is not None
                else requested
            )
            try:
                output = list(provider.extract(path, page_numbers=provider_pages))
            except Exception as error:  # pragma: no cover - provider-specific failures
                errors.append(error)
                continue
            if selected is None:
                selected = [str(item) for item in output]
                selected_pages = requested or list(range(1, len(selected) + 1))
            else:
                if len(output) != len(pending_indexes):
                    raise DocumentQualityError(
                        "pdf_ocr_page_mismatch",
                        "fallback OCR returned a different number of pages than requested",
                    )
                for index, candidate in zip(pending_indexes, output, strict=True):
                    if _ocr_text_score(str(candidate)) > _ocr_text_score(selected[index]):
                        selected[index] = str(candidate)
            pending_indexes = [
                index for index, text in enumerate(selected) if _ocr_text_needs_fallback(text)
            ]
            if not pending_indexes:
                break
        if selected is None:
            if errors:
                raise errors[-1]
            return []
        return selected

    def extract_layout(
        self,
        path: str | Path,
        *,
        page_numbers: Sequence[int] | None = None,
    ) -> list[OcrPageLayout]:
        requested = list(page_numbers) if page_numbers is not None else None
        selected: list[OcrPageLayout] | None = None
        selected_pages: list[int] | None = requested
        pending_indexes: list[int] = []
        errors: list[Exception] = []
        for provider in self.providers:
            extract_layout = getattr(provider, "extract_layout", None)
            if not callable(extract_layout):
                continue
            provider_pages = (
                None
                if selected is None and requested is None
                else [(selected_pages or [])[index] for index in pending_indexes]
                if selected is not None
                else requested
            )
            try:
                output = list(extract_layout(path, page_numbers=provider_pages))
            except Exception as error:  # pragma: no cover - provider-specific failures
                errors.append(error)
                continue
            if selected is None:
                selected = output
                selected_pages = requested or [page.page_number for page in selected]
            else:
                if len(output) != len(pending_indexes):
                    raise DocumentQualityError(
                        "pdf_ocr_page_mismatch",
                        "fallback OCR returned a different number of pages than requested",
                    )
                for index, candidate in zip(pending_indexes, output, strict=True):
                    if _ocr_layout_candidate_improves(selected[index], candidate):
                        selected[index] = candidate
            pending_indexes = [
                index
                for index, layout in enumerate(selected)
                if _ocr_layout_needs_fallback(
                    layout,
                    minimum_confidence=self.minimum_layout_confidence,
                )
            ]
            if not pending_indexes:
                break
        if selected is None:
            if errors:
                raise errors[-1]
            return []
        return selected


def _ocr_text_score(text: str) -> tuple[int, int]:
    quality = _document_quality([str(text)])
    return (
        0
        if (
            quality["corrupt_text_page_count"]
            or quality["fused_text_page_count"]
            or quality["lexical_damage_page_count"]
        )
        else 1,
        int(quality["non_whitespace_character_count"]),
    )


def _ocr_text_needs_fallback(text: str) -> bool:
    quality = _document_quality([str(text)])
    return bool(
        quality["corrupt_text_page_count"]
        or quality["fused_text_page_count"]
        or quality["lexical_damage_page_count"]
        or quality["non_whitespace_character_count"] < 24
    )


def _ocr_replacement_improves(original: str, candidate: str) -> bool:
    """Accept OCR only when it repairs quality without discarding most source text."""

    if _ocr_text_score(candidate) <= _ocr_text_score(original):
        return False
    original_size = len(re.sub(r"\s+", "", str(original)))
    candidate_size = len(re.sub(r"\s+", "", str(candidate)))
    if original_size < 24:
        return candidate_size >= 24
    return candidate_size >= max(24, int(original_size * 0.5))


def _ocr_layout_score(layout: OcrPageLayout) -> tuple[int, float, int]:
    text, _used_columns = _layout_reading_order_text(layout)
    base = _ocr_text_score(text)
    confidences = [float(block.confidence) for block in layout.blocks]
    average_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return (base[0], average_confidence, base[1])


def _ocr_layout_candidate_improves(
    original: OcrPageLayout,
    candidate: OcrPageLayout,
) -> bool:
    original_text, _original_columns = _layout_reading_order_text(original)
    candidate_text, _candidate_columns = _layout_reading_order_text(candidate)
    original_size = len(re.sub(r"\s+", "", original_text))
    candidate_size = len(re.sub(r"\s+", "", candidate_text))
    if original_size >= 24 and candidate_size < max(24, int(original_size * 0.5)):
        return False
    return _ocr_layout_score(candidate) > _ocr_layout_score(original)


def _ocr_layout_needs_fallback(
    layout: OcrPageLayout,
    *,
    minimum_confidence: float,
) -> bool:
    text, _used_columns = _layout_reading_order_text(layout)
    if _ocr_text_needs_fallback(text):
        return True
    confidences = [float(block.confidence) for block in layout.blocks]
    if not confidences:
        return True
    return sum(confidences) / len(confidences) < minimum_confidence


class PdfTextLayoutProvider:
    """Recover page geometry from an existing PDF text layer without OCR.

    Character coordinates keep independent columns separate even when the PDF's
    logical text order interleaves them.  Callers can fall back to image OCR when
    a page has no usable text layer or its critical fields are corrupt.
    """

    name = "pdf-text-layout"
    cache_profile = "pdf-text-layout:v1"

    def extract_layout(
        self,
        path: str | Path,
        *,
        page_numbers: Sequence[int] | None = None,
    ) -> list[OcrPageLayout]:
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise RuntimeError(
                "PDF text layout requires `pip install sagasmith-core[documents]`"
            ) from exc
        source = Path(path).expanduser().resolve()
        document = pdfium.PdfDocument(str(source))
        try:
            selected = list(range(1, len(document) + 1) if page_numbers is None else page_numbers)
            if any(not 1 <= page_number <= len(document) for page_number in selected):
                raise ValueError("PDF text-layout page number is outside the PDF")
            pages: list[OcrPageLayout] = []
            for page_number in selected:
                page = document[page_number - 1]
                try:
                    width, height = page.get_size()
                    text_page = page.get_textpage()
                    try:
                        blocks = _pdf_text_layout_blocks(
                            text_page,
                            page_height=float(height),
                        )
                    finally:
                        text_page.close()
                finally:
                    page.close()
                pages.append(
                    OcrPageLayout(
                        page_number=page_number,
                        width=max(1, round(width)),
                        height=max(1, round(height)),
                        blocks=tuple(blocks),
                    )
                )
            return pages
        finally:
            document.close()


def _pdf_text_layout_blocks(text_page: Any, *, page_height: float) -> list[OcrTextBlock]:
    """Group positioned PDF characters into column-preserving line blocks."""

    characters: list[dict[str, Any]] = []
    for index in range(text_page.count_chars()):
        character = text_page.get_text_range(index, 1)
        if not character or character in {"\r", "\n"}:
            continue
        try:
            left, bottom, right, top = text_page.get_charbox(index, loose=True)
        except Exception:
            continue
        if right <= left or top <= bottom:
            continue
        y0 = page_height - float(top)
        y1 = page_height - float(bottom)
        characters.append(
            {
                "text": character,
                "x0": float(left),
                "y0": y0,
                "x1": float(right),
                "y1": y1,
                "cy": (y0 + y1) / 2,
                "height": y1 - y0,
            }
        )
    if not characters:
        return []

    lines: list[dict[str, Any]] = []
    for character in sorted(characters, key=lambda item: (item["cy"], item["x0"])):
        candidate = None
        for line in reversed(lines[-24:]):
            tolerance = max(
                1.5,
                min(float(line["height"]), float(character["height"])) * 0.4,
            )
            if abs(float(line["cy"]) - float(character["cy"])) <= tolerance:
                candidate = line
                break
            if float(line["cy"]) < float(character["cy"]) - 24:
                break
        if candidate is None:
            candidate = {
                "cy": character["cy"],
                "height": character["height"],
                "cy_sum": 0.0,
                "height_sum": 0.0,
                "character_count": 0,
                "characters": [],
            }
            lines.append(candidate)
        candidate["characters"].append(character)
        candidate["cy_sum"] += float(character["cy"])
        candidate["height_sum"] += float(character["height"])
        candidate["character_count"] += 1
        candidate["cy"] = candidate["cy_sum"] / candidate["character_count"]
        candidate["height"] = candidate["height_sum"] / candidate["character_count"]

    blocks: list[OcrTextBlock] = []
    for line in sorted(lines, key=lambda item: (item["cy"], item["characters"][0]["x0"])):
        ordered = sorted(line["characters"], key=lambda item: item["x0"])
        segments: list[list[dict[str, Any]]] = [[]]
        for character in ordered:
            previous = segments[-1][-1] if segments[-1] else None
            gap = float(character["x0"]) - float(previous["x1"]) if previous else 0.0
            if previous is not None and gap > max(8.0, float(line["height"]) * 1.8):
                segments.append([])
            segments[-1].append(character)
        for segment in segments:
            reconstructed: list[str] = []
            previous = None
            for character in segment:
                value = str(character["text"])
                if previous is not None:
                    gap = float(character["x0"]) - float(previous["x1"])
                    spacing_threshold = max(
                        0.8,
                        float(line["height"]) * 0.12,
                    )
                    if (
                        gap > spacing_threshold
                        and not str(previous["text"]).isspace()
                        and not value.isspace()
                    ):
                        reconstructed.append(" ")
                reconstructed.append(value)
                previous = character
            raw_text = "".join(reconstructed)
            text = " ".join(raw_text.split())
            if not text:
                continue
            bad = sum(1 for character in text if unicodedata.category(character) in {"Co", "Cc"})
            confidence = max(0.4, 1.0 - bad / max(len(text), 1))
            blocks.append(
                OcrTextBlock(
                    text=text,
                    confidence=confidence,
                    x0=min(float(item["x0"]) for item in segment),
                    y0=min(float(item["y0"]) for item in segment),
                    x1=max(float(item["x1"]) for item in segment),
                    y1=max(float(item["y1"]) for item in segment),
                )
            )
    return blocks


def _layout_repairs_missing_word_spaces(embedded: str, layout: str) -> bool:
    """Prefer positioned text when it restores separators without changing glyphs."""

    embedded_letters = "".join(character for character in embedded if character.isalpha())
    layout_letters = "".join(character for character in layout if character.isalpha())
    if not embedded_letters or embedded_letters != layout_letters:
        return False
    embedded_words = len(re.findall(r"[^\W\d_]+", embedded, flags=re.UNICODE))
    layout_words = len(re.findall(r"[^\W\d_]+", layout, flags=re.UNICODE))
    return (
        layout_words >= embedded_words + 2
        and sum(character.isspace() for character in layout)
        >= sum(character.isspace() for character in embedded) + 2
    )


def _layout_reading_order_text(layout: OcrPageLayout) -> tuple[str, bool]:
    """Render positioned blocks in bounded one- through four-column order.

    PDF text APIs frequently interleave the left and right columns line by line.
    The text is syntactically valid, so glyph-quality checks cannot detect the
    semantic corruption.  This routine recognizes only a well-supported central
    gutter and otherwise keeps ordinary top-to-bottom order.  Full-width blocks
    divide the page into independent vertical bands so titles and cross-column
    tables are not moved behind a whole column.
    """

    blocks = [
        block
        for block in layout.blocks
        if block.text.strip() and block.x1 > block.x0 and block.y1 > block.y0
    ]
    if not blocks:
        return "", False
    width = float(layout.width)
    height = float(layout.height)
    # Character boxes can be expressed in an offset crop-box coordinate space.
    # Splitting at ``page_width / n`` then puts a nominal gutter inside the
    # left column (for example, a two-column page cropped 36 points from the
    # media box).  Use the occupied text rectangle for horizontal partitions;
    # ignore tiny folio marks so a page number cannot drag that rectangle back
    # to the media-box origin.
    extent_blocks = [
        block for block in blocks if block.x1 - block.x0 >= max(24.0, width * 0.08)
    ] or blocks
    horizontal_start = min(block.x0 for block in extent_blocks)
    horizontal_end = max(block.x1 for block in extent_blocks)
    usable_width = horizontal_end - horizontal_start
    if usable_width <= 0:
        horizontal_start = 0.0
        usable_width = width
    gutter = max(8.0, usable_width * 0.018)

    def vertical_extent(values: list[OcrTextBlock]) -> tuple[float, float]:
        return (
            min(block.y0 for block in values),
            max(block.y1 for block in values),
        )

    def vertical_bands(values: list[OcrTextBlock]) -> list[list[OcrTextBlock]]:
        ordered = sorted(values, key=lambda block: (block.y0, block.x0))
        bands: list[list[OcrTextBlock]] = []
        current: list[OcrTextBlock] = []
        current_bottom = 0.0
        gap_threshold = max(24.0, height * 0.035)
        for block in ordered:
            if current and block.y0 - current_bottom > gap_threshold:
                bands.append(current)
                current = []
            current.append(block)
            current_bottom = max(current_bottom, block.y1)
        if current:
            bands.append(current)
        return bands

    def column_partition(
        values: list[OcrTextBlock],
    ) -> tuple[list[list[OcrTextBlock]], list[OcrTextBlock]] | None:
        band_top = min(block.y0 for block in values)
        for column_count in (4, 3, 2):
            column_width = usable_width / column_count
            boundaries = [
                horizontal_start + column_width * index for index in range(1, column_count)
            ]
            columns: list[list[OcrTextBlock]] = [[] for _ in range(column_count)]
            spanning: list[OcrTextBlock] = []
            for block in values:
                center = (block.x0 + block.x1) / 2
                block_width = block.x1 - block.x0
                crosses_gutter = any(
                    block.x0 < boundary - gutter and block.x1 > boundary + gutter
                    for boundary in boundaries
                )
                has_row_peer = any(
                    other is not block and min(block.y1, other.y1) - max(block.y0, other.y0) > 0
                    for other in values
                )
                top_centered_display = (
                    block.y0 <= band_top + max(24.0, height * 0.04)
                    and abs(center - (horizontal_start + usable_width / 2)) <= usable_width * 0.12
                    and block_width >= usable_width * 0.08
                    and not has_row_peer
                )
                if (
                    block_width >= column_width * 1.45
                    or (crosses_gutter and block_width >= usable_width * 0.15)
                    or top_centered_display
                ):
                    spanning.append(block)
                    continue
                column_index = min(
                    column_count - 1,
                    max(0, int((center - horizontal_start) / column_width)),
                )
                columns[column_index].append(block)
            if len(spanning) > max(3, len(values) // 5):
                # A two-column page projected onto three equal bands makes most
                # ordinary lines look like cross-column dividers. Treat that as
                # evidence against the finer partition.
                continue
            if any(len(column) < 4 for column in columns):
                continue
            extents = [vertical_extent(column) for column in columns]
            overlap = min(end for _start, end in extents) - max(start for start, _end in extents)
            if overlap < max(36.0, height * 0.1):
                continue
            separated = all(
                median(block.x1 for block in columns[index]) + gutter
                < median(block.x0 for block in columns[index + 1])
                for index in range(column_count - 1)
            )
            if separated:
                return columns, spanning
        return None

    def order_partition(
        columns: list[list[OcrTextBlock]],
        spanning: list[OcrTextBlock],
    ) -> list[OcrTextBlock]:
        ordered_columns = [
            sorted(column, key=lambda block: (block.y0, block.x0)) for column in columns
        ]
        if len(columns) == 2 and not spanning:
            # A full-height primary column can wrap into the lower part of the
            # adjacent column while a short, floating sidebar occupies its top.
            # Reading each whole column in sequence puts that sidebar between an
            # unfinished sentence and its lowercase continuation.  Reorder only
            # when the geometry and punctuation both support that interpretation.
            primary = sorted(columns[0], key=lambda block: (block.y0, block.x0))
            adjacent = sorted(columns[1], key=lambda block: (block.y0, block.x0))
            adjacent_chunks: list[list[OcrTextBlock]] = []
            current_chunk: list[OcrTextBlock] = []
            current_bottom = 0.0
            chunk_gap = max(72.0, height * 0.1)
            for block in adjacent:
                if current_chunk and block.y0 - current_bottom > chunk_gap:
                    adjacent_chunks.append(current_chunk)
                    current_chunk = []
                current_chunk.append(block)
                current_bottom = max(current_bottom, block.y1)
            if current_chunk:
                adjacent_chunks.append(current_chunk)
            continuation = adjacent_chunks[-1] if len(adjacent_chunks) > 1 else []
            first_continuation = continuation[0].text.lstrip() if continuation else ""
            primary_tail = primary[-1].text.rstrip() if primary else ""
            if (
                primary_tail.endswith((",", "-", "–", "—"))
                and first_continuation[:1].islower()
            ):
                ordered_columns = [
                    primary,
                    [
                        block
                        for chunk in [continuation, *adjacent_chunks[:-1]]
                        for block in chunk
                    ],
                ]
        result: list[OcrTextBlock] = []
        remaining = [block for column in columns for block in column]
        for divider in sorted(spanning, key=lambda block: (block.y0, block.x0)):
            divider_center = (divider.y0 + divider.y1) / 2
            prior = [block for block in remaining if (block.y0 + block.y1) / 2 < divider_center]
            remaining = [block for block in remaining if block not in prior]
            prior_ids = {id(block) for block in prior}
            for column in ordered_columns:
                result.extend(block for block in column if id(block) in prior_ids)
            result.append(divider)
        remaining_ids = {id(block) for block in remaining}
        for column in ordered_columns:
            result.extend(block for block in column if id(block) in remaining_ids)
        return result

    ordered_blocks: list[OcrTextBlock] = []
    used_columns = False
    for band in vertical_bands(blocks):
        partition = column_partition(band)
        if partition is None:
            ordered_blocks.extend(sorted(band, key=lambda block: (block.y0, block.x0)))
            continue
        used_columns = True
        ordered_blocks.extend(order_partition(*partition))
    return "\n".join(block.text.strip() for block in ordered_blocks), used_columns


def ocr_layout_text(layout: OcrPageLayout) -> tuple[str, bool]:
    """Return stable reading-order text and whether column recovery was used."""

    return _layout_reading_order_text(layout)


def _ocr_page_layout(
    output: Any,
    *,
    page_number: int,
    image_shape: Sequence[int],
) -> OcrPageLayout:
    """Convert one OCR-engine result into a stable, serializable page layout."""

    if len(image_shape) < 2:
        raise ValueError("OCR image shape must include height and width")
    output_boxes = getattr(output, "boxes", None)
    output_texts = getattr(output, "txts", None)
    output_scores = getattr(output, "scores", None)
    raw_boxes = tuple(output_boxes) if output_boxes is not None else ()
    raw_texts = tuple(output_texts) if output_texts is not None else ()
    raw_scores = tuple(output_scores) if output_scores is not None else ()
    blocks: list[OcrTextBlock] = []
    for index, (raw_box, raw_text) in enumerate(zip(raw_boxes, raw_texts, strict=False)):
        text = str(raw_text).strip()
        if not text:
            continue
        points = [tuple(float(value) for value in point) for point in raw_box]
        if not points or any(len(point) != 2 for point in points):
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        confidence = float(raw_scores[index]) if index < len(raw_scores) else 0.0
        blocks.append(
            OcrTextBlock(
                text=text,
                confidence=confidence,
                x0=min(xs),
                y0=min(ys),
                x1=max(xs),
                y1=max(ys),
            )
        )
    return OcrPageLayout(
        page_number=page_number,
        width=int(image_shape[1]),
        height=int(image_shape[0]),
        blocks=tuple(blocks),
    )


def _visual_headings(
    text_page: Any,
    layout_profile: DocumentLayoutProfile = GENERIC_DOCUMENT_LAYOUT_PROFILE,
) -> list[tuple[str, int]]:
    """Recover headings from PDF font weight and rendered glyph height."""
    try:
        import ctypes

        import pypdfium2.raw as pdfium_c
    except ImportError:
        return []
    ranged = text_page.get_text_range(force_this=True)
    offset = 0
    styled: list[tuple[str, float, int, str]] = []

    def character_style(text_index: int) -> tuple[float, int, str] | None:
        char_index = pdfium_c.FPDFText_GetCharIndexFromTextIndex(text_page.raw, text_index)
        if char_index < 0:
            return None
        try:
            box = text_page.get_charbox(char_index)
            buffer = ctypes.create_string_buffer(256)
            flags = ctypes.c_long()
            pdfium_c.FPDFText_GetFontInfo(
                text_page.raw,
                char_index,
                buffer,
                len(buffer),
                ctypes.byref(flags),
            )
            return (
                float(box[3] - box[1]),
                int(pdfium_c.FPDFText_GetFontWeight(text_page.raw, char_index)),
                buffer.value.decode("utf-8", errors="replace"),
            )
        except Exception:
            return None

    for raw_line in ranged.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        letters = [index for index, char in enumerate(line) if char.isalpha()]
        if letters:
            sample_indices = sorted(
                {
                    letters[0],
                    letters[len(letters) // 2],
                    letters[-1],
                }
            )
            styles = [
                style
                for index in sample_indices
                if (style := character_style(offset + index)) is not None
            ]
            if styles:
                styled.append(
                    (
                        line.strip(),
                        median(style[0] for style in styles),
                        Counter(style[1] for style in styles).most_common(1)[0][0],
                        Counter(style[2] for style in styles).most_common(1)[0][0],
                    )
                )
        offset += len(raw_line)
    eligible = [
        (line, height, weight, font)
        for line, height, weight, font in styled
        if 3 <= len(line) <= 100 and not _PAGE_NUMBER_RE.fullmatch(line)
    ]
    if not eligible:
        return []
    body_height = median(height for _line, height, _weight, _font in eligible)
    body_weight = Counter(
        weight for line, _height, weight, _font in eligible if len(line) >= 20
    ).most_common(1)
    common_weight = (
        body_weight[0][0]
        if body_weight
        else Counter(weight for _line, _height, weight, _font in eligible).most_common(1)[0][0]
    )
    weights_informative = (
        bool(common_weight) and len({weight for _line, _height, weight, _font in eligible}) > 1
    )
    result: list[tuple[str, int]] = []
    for line, height, weight, font in eligible:
        if _TERMINAL_RE.search(line) or _LIST_RE.match(line):
            continue
        if _looks_like_corrupt_visual_heading(line):
            continue
        if layout_profile.excludes_visual_heading(line):
            continue
        ratio = height / max(body_height, 0.1)
        strong_size = height >= 8.0 and ratio >= 1.35
        small_caps = "smallcaps" in font.casefold() and height >= 7.0
        bold_display = (
            "bold" in font.casefold() and height >= 6.5 and _looks_like_letter_spaced_heading(line)
        )
        distinct_weight = weights_informative and weight != common_weight and height >= 7.0
        if not (strong_size or small_caps or bold_display or distinct_weight):
            continue
        level = 3 if ratio >= 1.8 else 4 if ratio >= 1.4 else 5
        result.append((line, level))
    return result


def _looks_like_corrupt_visual_heading(value: str) -> bool:
    """Reject decorative or handwritten glyph extraction as document structure."""
    quote_count = value.count("'") + value.count('"')
    return bool(
        any(char in value for char in ("\\", "~", "{", "}", "°", "•", "·"))
        or ".. " in value
        or quote_count >= 4
    )


def _extract_pdfium_pages(
    path: Path,
    layout_profile: DocumentLayoutProfile = GENERIC_DOCUMENT_LAYOUT_PROFILE,
) -> tuple[list[str], dict[int, list[tuple[str, int]]], list[int]]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError(
            "PDF conversion requires `pip install sagasmith-core[documents]`"
        ) from exc
    document = pdfium.PdfDocument(str(path))
    try:
        result: list[str] = []
        headings: dict[int, list[tuple[str, int]]] = {}
        layout_ordered_pages: list[int] = []
        for index in range(len(document)):
            page = document[index]
            try:
                width, height = page.get_size()
                text_page = page.get_textpage()
                try:
                    embedded_text = text_page.get_text_bounded() or ""
                    layout_text, used_columns = _layout_reading_order_text(
                        OcrPageLayout(
                            page_number=index + 1,
                            width=max(1, round(width)),
                            height=max(1, round(height)),
                            blocks=tuple(
                                _pdf_text_layout_blocks(
                                    text_page,
                                    page_height=float(height),
                                )
                            ),
                        )
                    )
                    use_layout = used_columns or _layout_repairs_missing_word_spaces(
                        embedded_text,
                        layout_text,
                    )
                    result.append(layout_text if use_layout else embedded_text)
                    if use_layout:
                        layout_ordered_pages.append(index + 1)
                    page_headings = _visual_headings(text_page, layout_profile)
                    if page_headings:
                        headings[index + 1] = page_headings
                finally:
                    text_page.close()
            finally:
                page.close()
        return result, headings, layout_ordered_pages
    finally:
        document.close()


def extract_pdf_page_text(path: str | Path, page_number: int) -> str:
    """Read one physical PDF page's embedded text layer without normalizing its order."""

    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError(
            "PDF extraction requires `pip install sagasmith-core[documents]`"
        ) from exc
    source = Path(path).expanduser().resolve()
    document = pdfium.PdfDocument(str(source))
    try:
        if page_number > len(document):
            raise ValueError("page_number is outside the PDF")
        page = document[page_number - 1]
        try:
            text_page = page.get_textpage()
            try:
                return text_page.get_text_bounded() or ""
            finally:
                text_page.close()
        finally:
            page.close()
    finally:
        document.close()


def _ocr_suspect_pages(
    provider: OcrProvider,
    source: Path,
    pages: list[str],
    page_numbers: Sequence[int],
) -> tuple[list[int], list[int]]:
    layout_pages: list[int] = []
    extract_layout = getattr(provider, "extract_layout", None)
    extracted: list[str]
    if callable(extract_layout):
        layouts = list(extract_layout(source, page_numbers=page_numbers))
        if len(layouts) != len(page_numbers):
            raise DocumentQualityError(
                "pdf_ocr_page_mismatch",
                "OCR provider returned a different number of pages than requested",
            )
        extracted = []
        for layout in layouts:
            text, used_columns = _layout_reading_order_text(layout)
            extracted.append(text)
            if used_columns:
                layout_pages.append(int(layout.page_number))
    else:
        extracted = provider.extract(source, page_numbers=page_numbers)
    if len(extracted) != len(page_numbers):
        raise DocumentQualityError(
            "pdf_ocr_page_mismatch",
            "OCR provider returned a different number of pages than requested",
        )
    replaced: list[int] = []
    for page_number, text in zip(page_numbers, extracted, strict=True):
        if str(text).strip():
            pages[page_number - 1] = str(text)
            replaced.append(page_number)
    return replaced, layout_pages


def _unmatched_text_bookmark_pages(
    page_texts: list[str],
    bookmarks: Sequence[DocumentBookmark],
) -> list[int]:
    """Find text-bearing outline targets whose extracted heading is unusable."""
    pages = [[_clean_line(line) for line in text.splitlines()] for text in page_texts]
    unmatched: set[int] = set()
    for bookmark in bookmarks:
        if (
            not 1 <= bookmark.page <= len(pages)
            or len(_bookmark_title(bookmark.title)) > _MAX_STRUCTURAL_HEADING_CHARS
            or not any(_normalize(line) for line in pages[bookmark.page - 1])
        ):
            continue
        if _match_bookmarks(pages, [bookmark])[1] == 0:
            unmatched.add(bookmark.page)
    return sorted(unmatched)


def _bookmark_ocr_candidate_pages(
    page_texts: list[str],
    bookmarks: Sequence[DocumentBookmark],
    layout_ordered_pages: Sequence[int],
) -> list[int]:
    """Bound outline-only OCR to short pages without stronger layout evidence.

    An outline mismatch on an otherwise dense text page is normally a missing
    display heading, not missing body content. Full-page OCR is both expensive
    and liable to destroy a recovered multi-column reading order. Sparse,
    corrupt, and fused pages remain independent mandatory OCR candidates.
    """

    layout_pages = set(layout_ordered_pages)
    return [
        page_number
        for page_number in _unmatched_text_bookmark_pages(page_texts, bookmarks)
        if page_number not in layout_pages
        and int(_page_quality(page_texts[page_number - 1])["non_whitespace_characters"])
        <= _BOOKMARK_OCR_MAX_NON_WHITESPACE
    ]


def _pdf_extraction_profile(
    ocr_provider: OcrProvider | None,
    layout_profile: DocumentLayoutProfile,
) -> str:
    ocr = getattr(ocr_provider, "cache_profile", None) or getattr(ocr_provider, "name", "none")
    return (
        f"pypdfium2:{_PDF_TEXT_EXTRACTOR_VERSION}:ocr={ocr}:layout={layout_profile.cache_identity}"
    )


def _pdf_extraction_cache_path(
    cache_dir: Path,
    checksum: str,
    profile: str,
) -> Path:
    profile_hash = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:12]
    return cache_dir / "pdf-pages" / checksum[:2] / f"{checksum}-{profile_hash}.json"


def _write_json_atomic(target: Path, value: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def _pdf_form_metadata(reader: Any) -> dict[str, Any]:
    """Preserve AcroForm evidence without treating blank templates as filled data."""
    try:
        fields = dict(reader.get_fields() or {})
    except Exception:
        fields = {}
    populated: dict[str, Any] = {}
    for raw_name, raw_field in fields.items():
        name = str(raw_name).strip()
        if not name or not isinstance(raw_field, dict):
            continue
        value = raw_field.get("/V")
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            rendered: Any = [str(item) for item in value]
        elif isinstance(value, (str, int, float, bool)):
            rendered = value
        else:
            rendered = str(value)
        if rendered in ("", "/Off", "Off", []):
            continue
        populated[name] = rendered
    return {
        "form_field_count": len(fields),
        "form_field_names": sorted(str(name) for name in fields),
        "populated_form_field_count": len(populated),
        "populated_form_fields": populated,
    }


class MarkdownDocumentConverter:
    def convert(
        self,
        path: str | Path,
        *,
        source_checksum: str | None = None,
    ) -> NormalizedDocument:
        source = Path(path).expanduser().resolve()
        content = source.read_text(encoding="utf-8")
        heading_count = len(re.findall(r"(?m)^#{1,6}\s+\S", content))
        return NormalizedDocument(
            content=content,
            media_type="text/markdown",
            source_path=str(source),
            checksum=source_checksum or file_sha256(source),
            warnings=("no structural headings were recovered",) if not heading_count else (),
            metadata={
                "normalizer_profile": "markdown",
                "normalizer_version": DOCUMENT_NORMALIZER_VERSION,
                "heading_count": heading_count,
            },
        )


class PdfDocumentConverter:
    def __init__(
        self,
        *,
        ocr_provider: OcrProvider | None = None,
        extraction_cache_dir: str | Path | None = None,
        layout_profile: DocumentLayoutProfile = GENERIC_DOCUMENT_LAYOUT_PROFILE,
    ) -> None:
        self.ocr_provider = ocr_provider
        self.extraction_cache_dir = (
            Path(extraction_cache_dir).expanduser().resolve()
            if extraction_cache_dir is not None
            else None
        )
        self.layout_profile = layout_profile

    def convert(
        self,
        path: str | Path,
        *,
        source_checksum: str | None = None,
    ) -> NormalizedDocument:
        source = Path(path).expanduser().resolve()
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError(
                "PDF conversion requires `pip install sagasmith-core[documents]`"
            ) from exc
        checksum = source_checksum or file_sha256(source)
        reader = PdfReader(str(source))
        bookmarks = self._bookmarks(reader)
        form_metadata = _pdf_form_metadata(reader)
        extraction_profile = _pdf_extraction_profile(
            self.ocr_provider,
            self.layout_profile,
        )
        extraction_target = (
            _pdf_extraction_cache_path(
                self.extraction_cache_dir,
                checksum,
                extraction_profile,
            )
            if self.extraction_cache_dir is not None
            else None
        )
        extracted = None
        if extraction_target is not None and extraction_target.is_file():
            try:
                cached = json.loads(extraction_target.read_text(encoding="utf-8"))
                cached_pages = [str(item) for item in cached["pages"]]
                if (
                    cached.get("schema") == _PDF_EXTRACTION_CACHE_SCHEMA
                    and cached.get("checksum") == checksum
                    and cached.get("profile") == extraction_profile
                    and cached.get("pages_checksum")
                    == hashlib.sha256("\x1e".join(cached_pages).encode("utf-8")).hexdigest()
                ):
                    extracted = (
                        cached_pages,
                        {
                            int(page): [(str(title), int(level)) for title, level in hints]
                            for page, hints in dict(cached.get("visual_headings") or {}).items()
                        },
                        dict(cached["initial_quality"]),
                        [int(item) for item in cached.get("ocr_pages", [])],
                        [int(item) for item in cached.get("layout_ordered_pages", [])],
                        [int(item) for item in cached.get("bookmark_ocr_pages", [])],
                        [int(item) for item in cached.get("ocr_rejected_pages", [])],
                    )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        extraction_cache_hit = extracted is not None
        if extracted is None:
            pages, visual_headings, layout_ordered_pages = _extract_pdfium_pages(
                source,
                self.layout_profile,
            )
            initial_quality = _document_quality(pages)
            corrupt_pages = list(initial_quality["corrupt_text_pages"])
            fused_pages = list(initial_quality["fused_text_pages"])
            lexical_damage_pages = list(initial_quality["lexical_damage_pages"])
            sparse_pages = list(initial_quality["sparse_pages"])
            bookmark_ocr_pages = _bookmark_ocr_candidate_pages(
                pages,
                bookmarks,
                layout_ordered_pages,
            )
            suspect_pages = sorted(
                set(corrupt_pages)
                | set(fused_pages)
                | set(lexical_damage_pages)
                | set(bookmark_ocr_pages)
                | (set(sparse_pages) if pages and len(sparse_pages) / len(pages) >= 0.8 else set())
            )
            ocr_pages: list[int] = []
            ocr_rejected_pages: list[int] = []
            if suspect_pages and self.ocr_provider is not None:
                candidate_pages = list(pages)
                attempted_ocr_pages, ocr_layout_pages = _ocr_suspect_pages(
                    self.ocr_provider, source, candidate_pages, suspect_pages
                )
                mandatory_ocr_pages = (
                    set(corrupt_pages)
                    | set(fused_pages)
                    | set(lexical_damage_pages)
                    | (
                        set(sparse_pages)
                        if pages and len(sparse_pages) / len(pages) >= 0.8
                        else set()
                    )
                )
                for page_number in attempted_ocr_pages:
                    accept = page_number in mandatory_ocr_pages and (
                        _ocr_replacement_improves(
                            pages[page_number - 1],
                            candidate_pages[page_number - 1],
                        )
                    )
                    if (
                        not accept
                        and page_number in bookmark_ocr_pages
                        and page_number not in layout_ordered_pages
                    ):
                        # Positioned extraction has already recovered a supported
                        # multi-column reading order.  A full-page OCR candidate
                        # can match a damaged outline title while silently
                        # interleaving those columns, so an outline-only gain is
                        # not sufficient evidence to replace the stronger text.
                        page_bookmarks = [
                            bookmark for bookmark in bookmarks if bookmark.page == page_number
                        ]
                        before = _match_bookmarks(
                            [[_clean_line(line) for line in text.splitlines()] for text in pages],
                            page_bookmarks,
                        )[1]
                        after = _match_bookmarks(
                            [
                                [_clean_line(line) for line in text.splitlines()]
                                for text in candidate_pages
                            ],
                            page_bookmarks,
                        )[1]
                        accept = after > before
                    if accept:
                        pages[page_number - 1] = candidate_pages[page_number - 1]
                        ocr_pages.append(page_number)
                    else:
                        ocr_rejected_pages.append(page_number)
                layout_ordered_pages = sorted(
                    set(layout_ordered_pages) | (set(ocr_layout_pages) & set(ocr_pages))
                )
                for page_number in ocr_pages:
                    visual_headings.pop(page_number, None)
            if extraction_target is not None:
                _write_json_atomic(
                    extraction_target,
                    {
                        "schema": _PDF_EXTRACTION_CACHE_SCHEMA,
                        "checksum": checksum,
                        "profile": extraction_profile,
                        "pages_checksum": hashlib.sha256(
                            "\x1e".join(pages).encode("utf-8")
                        ).hexdigest(),
                        "pages": pages,
                        "visual_headings": {
                            str(page): hints for page, hints in visual_headings.items()
                        },
                        "initial_quality": initial_quality,
                        "ocr_pages": ocr_pages,
                        "bookmark_ocr_pages": bookmark_ocr_pages,
                        "ocr_rejected_pages": ocr_rejected_pages,
                        "layout_ordered_pages": layout_ordered_pages,
                    },
                )
        else:
            (
                pages,
                visual_headings,
                initial_quality,
                ocr_pages,
                layout_ordered_pages,
                bookmark_ocr_pages,
                ocr_rejected_pages,
            ) = extracted
        pages, pdf_word_break_repair_count = _repair_pdf_word_break_noncharacters(pages)
        pages, pdf_control_artifact_repair_count = _repair_pdf_control_artifacts(pages)
        quality = _document_quality(pages)
        if pages and quality["suspect_page_count"] / len(pages) >= 0.8:
            if self.ocr_provider is None:
                raise DocumentQualityError(
                    "pdf_text_unavailable",
                    "PDF has no usable text layer; configure an OCR provider",
                )
            raise DocumentQualityError(
                "pdf_ocr_unusable",
                "OCR did not recover usable text from at least 80% of PDF pages",
            )

        content, stats, structure_warnings = build_structured_markdown(
            pages,
            bookmarks,
            visual_headings,
            self.layout_profile,
        )
        warnings = list(structure_warnings)
        unresolved_corrupt = list(quality["corrupt_text_pages"])
        if unresolved_corrupt:
            warnings.append(
                f"text layer remains corrupt on {len(unresolved_corrupt)}/{len(pages)} pages"
            )
        unresolved_lexical_damage = list(quality["lexical_damage_pages"])
        if unresolved_lexical_damage:
            warnings.append(
                "text layer remains lexically damaged on "
                f"{len(unresolved_lexical_damage)}/{len(pages)} pages"
            )
        if quality["text_page_coverage"] < 0.9:
            warnings.append(
                f"usable text covers only {quality['text_page_count']}/{len(pages)} pages"
            )
        return NormalizedDocument(
            content=content,
            media_type="application/pdf",
            source_path=str(source),
            checksum=checksum,
            page_count=len(pages),
            bookmarks=tuple(bookmarks),
            warnings=tuple(warnings),
            metadata={
                **stats,
                "normalizer_profile": "pdf-layout",
                "normalizer_version": DOCUMENT_NORMALIZER_VERSION,
                "text_extractor": "pypdfium2",
                "text_extractor_version": _PDF_TEXT_EXTRACTOR_VERSION,
                "outline_extractor": "pypdf",
                **form_metadata,
                "ocr_provider": self.ocr_provider.name if ocr_pages else None,
                "ocr_profile": (
                    str(getattr(self.ocr_provider, "cache_profile", None) or self.ocr_provider.name)
                    if ocr_pages and self.ocr_provider is not None
                    else None
                ),
                "ocr_pages": ocr_pages,
                "bookmark_ocr_pages": bookmark_ocr_pages,
                "ocr_rejected_pages": ocr_rejected_pages,
                "layout_ordered_pages": layout_ordered_pages,
                "pdf_word_break_repair_count": pdf_word_break_repair_count,
                "pdf_control_artifact_repair_count": pdf_control_artifact_repair_count,
                "extraction_cache_hit": extraction_cache_hit,
                "initial_quality": initial_quality,
                "quality": quality,
            },
        )

    @staticmethod
    def _bookmarks(reader: Any) -> list[DocumentBookmark]:
        result: list[DocumentBookmark] = []

        def walk(items: list[Any], depth: int = 0) -> None:
            for item in items:
                if isinstance(item, list):
                    walk(item, depth + 1)
                    continue
                try:
                    page = reader.get_destination_page_number(item) + 1
                except Exception:
                    continue
                title = str(getattr(item, "title", item)).strip()
                if title:
                    result.append(DocumentBookmark(title, page, depth))

        outline = getattr(reader, "outline", [])
        if isinstance(outline, list):
            walk(outline)
        return result


def _cache_profile(
    path: Path,
    ocr_provider: OcrProvider | None,
    layout_profile: DocumentLayoutProfile,
) -> str:
    if path.suffix.casefold() == ".pdf":
        return (
            f"pdf-layout:{DOCUMENT_NORMALIZER_VERSION}:"
            f"extraction={_pdf_extraction_profile(ocr_provider, layout_profile)}"
        )
    return f"markdown:{DOCUMENT_NORMALIZER_VERSION}"


def _cache_path(cache_dir: Path, checksum: str, profile: str) -> Path:
    profile_hash = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:12]
    return cache_dir / checksum[:2] / f"{checksum}-{profile_hash}.json"


def _normalization_cache_lock(target: Path) -> RLock:
    """Return the process-local in-flight lock for one immutable cache key."""
    with _NORMALIZATION_CACHE_LOCKS_GUARD:
        lock = _NORMALIZATION_CACHE_LOCKS.get(target)
        if lock is None:
            lock = RLock()
            _NORMALIZATION_CACHE_LOCKS[target] = lock
        return lock


def _read_normalized_document_cache(
    target: Path,
    *,
    source: Path,
    checksum: str,
    profile: str,
) -> NormalizedDocument | None:
    if not target.is_file():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        content = str(value["content"])
        if (
            value.get("schema") == _DOCUMENT_CACHE_SCHEMA
            and value.get("checksum") == checksum
            and value.get("profile") == profile
            and value.get("content_checksum")
            == hashlib.sha256(content.encode("utf-8")).hexdigest()
        ):
            return NormalizedDocument(
                content=content,
                media_type=str(value["media_type"]),
                source_path=str(source),
                checksum=checksum,
                page_count=int(value.get("page_count", 1)),
                bookmarks=tuple(
                    DocumentBookmark(str(item["title"]), int(item["page"]), int(item["depth"]))
                    for item in value.get("bookmarks", [])
                ),
                warnings=tuple(str(item) for item in value.get("warnings", [])),
                metadata={
                    **dict(value.get("metadata") or {}),
                    "normalization_cache_hit": True,
                },
            )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return None


def _normalize_document_with_cache(
    source: Path,
    *,
    checksum: str,
    profile: str,
    target: Path | None,
    ocr_provider: OcrProvider | None,
    cache_dir: str | Path | None,
    layout_profile: DocumentLayoutProfile,
) -> NormalizedDocument:
    if target is not None:
        cached = _read_normalized_document_cache(
            target,
            source=source,
            checksum=checksum,
            profile=profile,
        )
        if cached is not None:
            return cached

    document = converter_for(
        source,
        ocr_provider=ocr_provider,
        extraction_cache_dir=cache_dir,
        layout_profile=layout_profile,
    ).convert(
        source,
        source_checksum=checksum,
    )
    document = NormalizedDocument(
        content=document.content,
        media_type=document.media_type,
        source_path=document.source_path,
        checksum=document.checksum,
        page_count=document.page_count,
        bookmarks=document.bookmarks,
        warnings=document.warnings,
        metadata={**document.metadata, "normalization_cache_hit": False},
    )
    if target is not None:
        value = {
            "schema": _DOCUMENT_CACHE_SCHEMA,
            "profile": profile,
            "checksum": checksum,
            "content_checksum": hashlib.sha256(document.content.encode("utf-8")).hexdigest(),
            "content": document.content,
            "media_type": document.media_type,
            "page_count": document.page_count,
            "bookmarks": [
                {"title": item.title, "page": item.page, "depth": item.depth}
                for item in document.bookmarks
            ],
            "warnings": list(document.warnings),
            "metadata": document.metadata,
        }
        _write_json_atomic(target, value)
    return document


def normalize_document(
    path: str | Path,
    *,
    ocr_provider: OcrProvider | None = None,
    cache_dir: str | Path | None = None,
    expected_checksum: str | None = None,
    layout_profile: DocumentLayoutProfile = GENERIC_DOCUMENT_LAYOUT_PROFILE,
) -> NormalizedDocument:
    """Convert a document once and reuse a content-addressed normalized form."""
    source = Path(path).expanduser().resolve()
    checksum = file_sha256(source)
    if expected_checksum and checksum != expected_checksum:
        raise DocumentQualityError(
            "source_checksum_mismatch",
            "managed document checksum no longer matches its staged import job",
        )
    profile = _cache_profile(source, ocr_provider, layout_profile)
    target = (
        _cache_path(Path(cache_dir).expanduser().resolve(), checksum, profile)
        if cache_dir is not None
        else None
    )
    if target is None:
        return _normalize_document_with_cache(
            source,
            checksum=checksum,
            profile=profile,
            target=None,
            ocr_provider=ocr_provider,
            cache_dir=cache_dir,
            layout_profile=layout_profile,
        )
    cached = _read_normalized_document_cache(
        target,
        source=source,
        checksum=checksum,
        profile=profile,
    )
    if cached is not None:
        return cached
    with _normalization_cache_lock(target):
        return _normalize_document_with_cache(
            source,
            checksum=checksum,
            profile=profile,
            target=target,
            ocr_provider=ocr_provider,
            cache_dir=cache_dir,
            layout_profile=layout_profile,
        )


def render_pdf_page(
    path: str | Path,
    page_number: int,
    *,
    scale: float = 1.5,
) -> RenderedDocumentPage:
    """Render one 1-based PDF page without weakening text-parser boundaries.

    Rendering is deliberately separate from structural text conversion.  It is
    intended for maps, diagrams, handouts, and other visual evidence that an
    importing agent or human must review explicitly before deriving structure.
    """
    source = Path(path).expanduser().resolve()
    if source.suffix.casefold() != ".pdf" or not source.is_file():
        raise ValueError("page rendering requires an existing PDF file")
    if not isinstance(page_number, int) or isinstance(page_number, bool):
        raise TypeError("page_number must be a 1-based integer")
    if not 0.5 <= scale <= 4.0:
        raise ValueError("scale must be between 0.5 and 4.0")
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError(
            "PDF page rendering requires `pip install sagasmith-core[documents]`"
        ) from exc

    document = pdfium.PdfDocument(str(source))
    try:
        page_count = len(document)
        if not 1 <= page_number <= page_count:
            raise ValueError(f"page_number must be between 1 and {page_count}")
        page = document[page_number - 1]
        try:
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil()
                from io import BytesIO

                output = BytesIO()
                image.save(output, format="PNG", optimize=True)
                content = output.getvalue()
                width, height = image.size
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()
    return RenderedDocumentPage(
        content=content,
        media_type="image/png",
        source_path=str(source),
        source_checksum=file_sha256(source),
        page_number=page_number,
        page_count=page_count,
        width=width,
        height=height,
        scale=float(scale),
        checksum=hashlib.sha256(content).hexdigest(),
    )


def converter_for(
    path: str | Path,
    *,
    ocr_provider: OcrProvider | None = None,
    extraction_cache_dir: str | Path | None = None,
    layout_profile: DocumentLayoutProfile = GENERIC_DOCUMENT_LAYOUT_PROFILE,
):
    suffix = Path(path).suffix.casefold()
    if suffix == ".pdf":
        return PdfDocumentConverter(
            ocr_provider=ocr_provider,
            extraction_cache_dir=extraction_cache_dir,
            layout_profile=layout_profile,
        )
    if suffix in DOCUMENT_SOURCE_SUFFIXES - {".pdf"}:
        return MarkdownDocumentConverter()
    raise ValueError(f"unsupported document type: {suffix}")


def page_for_offset(content: str, offset: int) -> int | None:
    return PageLocator(content).page_for_offset(offset)


def strip_page_markers(value: str) -> str:
    return "\n".join(
        line for line in value.splitlines() if not _PAGE_MARKER_RE.match(line.strip())
    ).strip()
