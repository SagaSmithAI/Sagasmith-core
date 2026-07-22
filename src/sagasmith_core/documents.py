"""Document conversion contracts and layout-aware PDF normalization."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Any, Protocol
from uuid import uuid4

DOCUMENT_NORMALIZER_VERSION = "13"
_DOCUMENT_CACHE_SCHEMA = 1
_PDF_EXTRACTION_CACHE_SCHEMA = 1
_PDF_TEXT_EXTRACTOR_VERSION = "3"


class DocumentQualityError(RuntimeError):
    """Raised when a source cannot provide enough content to parse safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
_ROOM_RE = re.compile(r"^[A-Z]{1,3}\d+[A-Za-z]?\s*[.．]\s*\S+")
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
            (match.start(), int(match.group(1)))
            for match in _PAGE_MARKER_SCAN_RE.finditer(content)
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


def _looks_letter_spaced(value: str) -> bool:
    """Recognize display-font extraction that splits words into letter tokens."""
    words = re.findall(r"[A-Za-z]+", value)
    singles = sum(len(word) == 1 for word in words)
    return singles >= 3 and singles / max(len(words), 1) >= 0.3


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
                and SequenceMatcher(None, raw_normalized, canonical_normalized).ratio()
                >= 0.88
            )
        )
    )


def _chapter_identity(value: str) -> str:
    """Compare abbreviated and full chapter labels as the same boundary."""
    text = value.strip()
    text = re.sub(
        r"^Ch(?:apter)?\s*\.?\s*", "chapter ", text, flags=re.IGNORECASE
    )
    text = re.sub(
        r"^App(?:endix)?\s*\.?\s*", "appendix ", text, flags=re.IGNORECASE
    )
    return _normalize(text)


def _chapter_label(value: str) -> str | None:
    identity = _chapter_identity(value)
    matched = re.match(
        r"^(chapter|appendix|part|episodes?)(\d+(?:and\d+)?|[a-z])", identity
    )
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


def _repeated_margin_lines(pages: list[list[str]]) -> set[str]:
    candidates: Counter[str] = Counter()
    for lines in pages:
        nonempty = [line for line in lines if line]
        seen_on_page: set[str] = set()
        for line in [*nonempty[:3], *nonempty[-3:]]:
            if _CHAPTER_RE.match(line):
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
        target = _normalize(bookmark.title)
        structural_bookmark = bool(_CHAPTER_RE.match(bookmark.title.strip()))
        best_index = -1
        best_score = 0.0
        for index, line in enumerate(pages[bookmark.page - 1]):
            if (
                structural_bookmark
                and not _CHAPTER_RE.match(line)
                and not _looks_letter_spaced(line)
            ):
                continue
            candidate = _normalize(line)
            if not target or not candidate:
                continue
            if target in candidate or candidate in target:
                score = max(
                    0.9,
                    min(len(target), len(candidate)) / max(len(target), len(candidate)),
                )
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
            nonempty_before = sum(
                bool(line)
                for line in pages[bookmark.page - 1][:best_index]
            )
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
            if _prefer_bookmark_title(
                raw_title, bookmark.title, trusted=trusted_chapter
            ):
                canonical_titles[key] = _bookmark_title(bookmark.title)
            matched += 1
        elif (
            structural_depth is not None
            and bookmark.depth == structural_depth
            and structural_bookmark
        ):
            title = _bookmark_title(bookmark.title)
            page_titles = synthetic_chapters.setdefault(bookmark.page, [])
            if _chapter_identity(title) not in {
                _chapter_identity(item) for item in page_titles
            }:
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
    return bool(
        3 <= len(text) <= 80
        and 1 <= len(text.split()) <= 12
        and letters
        and not uncased_letters
        and all(char == char.upper() for char in letters)
        and not _TERMINAL_RE.search(text)
    )


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

    def flush() -> None:
        if not paragraph:
            return
        merged = paragraph[0]
        for line in paragraph[1:]:
            if merged.endswith("-") and line[:1].isascii() and line[:1].isalpha():
                merged = merged[:-1] + line
            else:
                merged += _joiner(merged, line) + line
        output.extend((merged, ""))
        paragraph.clear()

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
        display_line = canonical_titles.get(key, line)
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
                len(trusted) >= 12
                and len(identity) > len(trusted)
                and identity.startswith(trusted)
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
            not trusted_top_level
            and _CHAPTER_RE.match(display_line)
            and duplicate_trusted_chapter
        ):
            # Drop duplicated running headers or visual recovery of a boundary
            # already anchored by an outline entry elsewhere in the document.
            continue
        if structural_headings and _CHAPTER_RE.match(display_line) and (
            trusted_top_level
            or (
                _looks_like_automatic_chapter_heading(display_line)
                and (
                    level is not None
                    or chapter_confirmation
                    or (index in top_lines and chapter_lines == 1)
                )
            )
        ):
            level = 1
        elif structural_headings and _ROOM_RE.match(display_line):
            level = level or 4
            room_count += 1
        elif structural_headings and level is None and _looks_like_all_caps_heading(display_line):
            level = 5
        if level is not None:
            flush()
            output.extend((f"{'#' * level} {display_line}", ""))
            heading_count += 1
        elif _LIST_RE.match(line):
            flush()
            output.append(re.sub(r"^[•●▪◼]\s*", "- ", line))
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
) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    """Normalize extracted PDF pages into provenance-preserving Markdown."""
    bookmarks = bookmarks or []
    pages = [[_clean_line(line) for line in text.splitlines()] for text in page_texts]
    repeated = _repeated_margin_lines(pages)
    (
        heading_levels,
        matched,
        trusted_chapters,
        canonical_titles,
        synthetic_chapters,
    ) = _match_bookmarks(pages, bookmarks)
    trusted_chapter_titles = {
        _chapter_identity(canonical_titles.get(key, pages[key[0] - 1][key[1]]))
        for key in trusted_chapters
    }
    trusted_chapter_titles.update(
        _chapter_identity(title)
        for titles in synthetic_chapters.values()
        for title in titles
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
    if bookmarks and matched / len(bookmarks) < 0.95:
        warnings.append(
            f"bookmark match rate is {matched}/{len(bookmarks)}; expected at least 95%"
        )
    if heading_count == 0:
        warnings.append("no structural headings were recovered")
    content = "\n".join(output).strip() + "\n"
    return (
        content,
        {
            "bookmark_count": len(bookmarks),
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
    private_use = sum(unicodedata.category(char) == "Co" for char in text)
    control = sum(
        unicodedata.category(char) == "Cc" and char not in "\t\r\n"
        for char in text
    )
    replacement = text.count("\ufffd")
    denominator = max(characters, 1)
    return {
        "characters": characters,
        "non_whitespace_characters": non_whitespace,
        "private_use_characters": private_use,
        "control_characters": control,
        "replacement_characters": replacement,
        "private_use_ratio": private_use / denominator,
        "control_ratio": control / denominator,
        "replacement_ratio": replacement / denominator,
        "sparse": non_whitespace < 20,
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
    suspect_pages = sorted(set(sparse_pages) | set(corrupt_pages))
    return {
        "character_count": characters,
        "non_whitespace_character_count": non_whitespace,
        "text_page_count": len(page_stats) - len(sparse_pages),
        "sparse_page_count": len(sparse_pages),
        "sparse_pages": sparse_pages,
        "corrupt_text_page_count": len(corrupt_pages),
        "corrupt_text_pages": corrupt_pages,
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


class RapidOcrProvider:
    """Lazy local OCR for pages whose PDF text layer is empty or corrupt."""

    name = "rapidocr"

    def __init__(self, *, scale: float = 2.0) -> None:
        if not 1.0 <= scale <= 4.0:
            raise ValueError("OCR scale must be between 1.0 and 4.0")
        self.scale = float(scale)
        self._engine: Any | None = None

    @property
    def cache_profile(self) -> str:
        return f"{self.name}:scale={self.scale:.2f}"

    def extract(
        self,
        path: str | Path,
        *,
        page_numbers: Sequence[int] | None = None,
    ) -> list[str]:
        try:
            import pypdfium2 as pdfium
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise RuntimeError(
                "OCR requires `pip install sagasmith-core[documents,ocr]`"
            ) from exc
        if self._engine is None:
            self._engine = RapidOCR()
        source = Path(path).expanduser().resolve()
        document = pdfium.PdfDocument(str(source))
        try:
            selected = list(page_numbers or range(1, len(document) + 1))
            if any(not 1 <= page_number <= len(document) for page_number in selected):
                raise ValueError("OCR page number is outside the PDF")
            result: list[str] = []
            for page_number in selected:
                page = document[page_number - 1]
                try:
                    bitmap = page.render(scale=self.scale)
                    try:
                        output = self._engine(bitmap.to_numpy())
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                texts = tuple(getattr(output, "txts", ()) or ())
                result.append("\n".join(str(item).strip() for item in texts if str(item).strip()))
            return result
        finally:
            document.close()


def _visual_headings(text_page: Any) -> list[tuple[str, int]]:
    """Recover headings from PDF font weight and rendered glyph height."""
    try:
        import ctypes

        import pypdfium2.raw as pdfium_c
    except ImportError:
        return []
    ranged = text_page.get_text_range(force_this=True)
    offset = 0
    styled: list[tuple[str, float, int, str]] = []
    for raw_line in ranged.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        first_letter = next((index for index, char in enumerate(line) if char.isalpha()), None)
        if first_letter is not None:
            char_index = pdfium_c.FPDFText_GetCharIndexFromTextIndex(
                text_page.raw, offset + first_letter
            )
            if char_index >= 0:
                try:
                    box = text_page.get_charbox(char_index)
                    height = float(box[3] - box[1])
                    weight = int(pdfium_c.FPDFText_GetFontWeight(text_page.raw, char_index))
                    buffer = ctypes.create_string_buffer(256)
                    flags = ctypes.c_long()
                    pdfium_c.FPDFText_GetFontInfo(
                        text_page.raw,
                        char_index,
                        buffer,
                        len(buffer),
                        ctypes.byref(flags),
                    )
                    styled.append(
                        (
                            line.strip(),
                            height,
                            weight,
                            buffer.value.decode("utf-8", errors="replace"),
                        )
                    )
                except Exception:
                    pass
        offset += len(raw_line)
    eligible = [
        (line, height, weight, font)
        for line, height, weight, font in styled
        if 3 <= len(line) <= 160 and not _PAGE_NUMBER_RE.fullmatch(line)
    ]
    if not eligible:
        return []
    body_height = median(height for _line, height, _weight, _font in eligible)
    body_weight = Counter(
        weight for line, _height, weight, _font in eligible if len(line) >= 20
    ).most_common(1)
    common_weight = body_weight[0][0] if body_weight else Counter(
        weight for _line, _height, weight, _font in eligible
    ).most_common(1)[0][0]
    weights_informative = bool(common_weight) and len(
        {weight for _line, _height, weight, _font in eligible}
    ) > 1
    result: list[tuple[str, int]] = []
    field_label = re.compile(
        r"(?i)^(?:armor|weapons|tools|skills|saving throws|hit dice|hit points at|"
        r"casting time|range|components|duration)\s*:"
    )
    for line, height, weight, font in eligible:
        if _TERMINAL_RE.search(line) or _LIST_RE.match(line):
            continue
        if field_label.match(line):
            continue
        ratio = height / max(body_height, 0.1)
        strong_size = height >= 8.0 and ratio >= 1.35
        small_caps = "smallcaps" in font.casefold() and height >= 7.0
        distinct_weight = weights_informative and weight != common_weight and height >= 7.0
        if not (strong_size or small_caps or distinct_weight):
            continue
        level = 3 if ratio >= 1.8 else 4 if ratio >= 1.4 else 5
        result.append((line, level))
    return result


def _extract_pdfium_pages(path: Path) -> tuple[list[str], dict[int, list[tuple[str, int]]]]:
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
        for index in range(len(document)):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    result.append(text_page.get_text_bounded() or "")
                    page_headings = _visual_headings(text_page)
                    if page_headings:
                        headings[index + 1] = page_headings
                finally:
                    text_page.close()
            finally:
                page.close()
        return result, headings
    finally:
        document.close()


def _ocr_suspect_pages(
    provider: OcrProvider,
    source: Path,
    pages: list[str],
    page_numbers: Sequence[int],
) -> list[int]:
    try:
        extracted = provider.extract(source, page_numbers=page_numbers)
    except TypeError:
        # Compatibility with providers implementing the original all-pages contract.
        all_pages = provider.extract(source)
        extracted = [all_pages[page_number - 1] for page_number in page_numbers]
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
    return replaced


def _pdf_extraction_profile(ocr_provider: OcrProvider | None) -> str:
    ocr = getattr(ocr_provider, "cache_profile", None) or getattr(
        ocr_provider, "name", "none"
    )
    return f"pypdfium2:{_PDF_TEXT_EXTRACTOR_VERSION}:ocr={ocr}"


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
    ) -> None:
        self.ocr_provider = ocr_provider
        self.extraction_cache_dir = (
            Path(extraction_cache_dir).expanduser().resolve()
            if extraction_cache_dir is not None
            else None
        )

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
        extraction_profile = _pdf_extraction_profile(self.ocr_provider)
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
                    )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        extraction_cache_hit = extracted is not None
        if extracted is None:
            pages, visual_headings = _extract_pdfium_pages(source)
            initial_quality = _document_quality(pages)
            corrupt_pages = list(initial_quality["corrupt_text_pages"])
            sparse_pages = list(initial_quality["sparse_pages"])
            suspect_pages = sorted(
                set(corrupt_pages)
                | (
                    set(sparse_pages)
                    if pages and len(sparse_pages) / len(pages) >= 0.8
                    else set()
                )
            )
            ocr_pages: list[int] = []
            if suspect_pages and self.ocr_provider is not None:
                ocr_pages = _ocr_suspect_pages(
                    self.ocr_provider, source, pages, suspect_pages
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
                    },
                )
        else:
            pages, visual_headings, initial_quality, ocr_pages = extracted
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

        reader = PdfReader(str(source))
        bookmarks = self._bookmarks(reader)
        form_metadata = _pdf_form_metadata(reader)
        content, stats, structure_warnings = build_structured_markdown(
            pages,
            bookmarks,
            visual_headings,
        )
        warnings = list(structure_warnings)
        unresolved_corrupt = list(quality["corrupt_text_pages"])
        if unresolved_corrupt:
            warnings.append(
                "text layer remains corrupt on "
                f"{len(unresolved_corrupt)}/{len(pages)} pages"
            )
        if quality["text_page_coverage"] < 0.9:
            warnings.append(
                "usable text covers only "
                f"{quality['text_page_count']}/{len(pages)} pages"
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
                "outline_extractor": "pypdf",
                **form_metadata,
                "ocr_provider": self.ocr_provider.name if ocr_pages else None,
                "ocr_pages": ocr_pages,
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


def _cache_profile(path: Path, ocr_provider: OcrProvider | None) -> str:
    if path.suffix.casefold() == ".pdf":
        ocr = getattr(ocr_provider, "cache_profile", None) or getattr(
            ocr_provider, "name", "none"
        )
        return f"pdf-layout:{DOCUMENT_NORMALIZER_VERSION}:ocr={ocr}"
    return f"markdown:{DOCUMENT_NORMALIZER_VERSION}"


def _cache_path(cache_dir: Path, checksum: str, profile: str) -> Path:
    profile_hash = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:12]
    return cache_dir / checksum[:2] / f"{checksum}-{profile_hash}.json"


def normalize_document(
    path: str | Path,
    *,
    ocr_provider: OcrProvider | None = None,
    cache_dir: str | Path | None = None,
    expected_checksum: str | None = None,
) -> NormalizedDocument:
    """Convert a document once and reuse a content-addressed normalized form."""
    source = Path(path).expanduser().resolve()
    checksum = file_sha256(source)
    if expected_checksum and checksum != expected_checksum:
        raise DocumentQualityError(
            "source_checksum_mismatch",
            "managed document checksum no longer matches its staged import job",
        )
    profile = _cache_profile(source, ocr_provider)
    target = (
        _cache_path(Path(cache_dir).expanduser().resolve(), checksum, profile)
        if cache_dir is not None
        else None
    )
    if target is not None and target.is_file():
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
                        DocumentBookmark(
                            str(item["title"]), int(item["page"]), int(item["depth"])
                        )
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

    document = converter_for(
        source,
        ocr_provider=ocr_provider,
        extraction_cache_dir=cache_dir,
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
):
    suffix = Path(path).suffix.casefold()
    if suffix == ".pdf":
        return PdfDocumentConverter(
            ocr_provider=ocr_provider,
            extraction_cache_dir=extraction_cache_dir,
        )
    if suffix in {".md", ".markdown", ".txt"}:
        return MarkdownDocumentConverter()
    raise ValueError(f"unsupported document type: {suffix}")


def page_for_offset(content: str, offset: int) -> int | None:
    return PageLocator(content).page_for_offset(offset)


def strip_page_markers(value: str) -> str:
    return "\n".join(
        line for line in value.splitlines() if not _PAGE_MARKER_RE.match(line.strip())
    ).strip()
