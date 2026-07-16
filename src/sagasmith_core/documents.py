"""Document conversion contracts and layout-aware PDF normalization."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol


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


class DocumentConverter(Protocol):
    def convert(self, path: str | Path) -> NormalizedDocument: ...


class OcrProvider(Protocol):
    name: str

    def extract(self, path: str | Path) -> list[str]: ...


_CHAPTER_RE = re.compile(
    r"^(?:(?:第[一二三四五六七八九十百0-9]+章|附录\s*[A-ZＡ-Ｚ])(?:\s|：|:)|"
    r"(?:Chapter|Appendix)\s+[0-9A-Z]+(?:\s|:))",
    re.IGNORECASE,
)
_ROOM_RE = re.compile(r"^[A-Z]{1,3}\d+[A-Za-z]?\s*[.．]\s*\S+")
_LIST_RE = re.compile(r"^(?:[-*•●▪◼]|\d+[.)、]|[A-Za-z][.)])\s*")
_PAGE_NUMBER_RE = re.compile(r"^\d{1,4}$")
_TERMINAL_RE = re.compile(r"[。！？!?；;：:…][”’』」）》】]*$")
_PAGE_MARKER_RE = re.compile(r"^<!-- page: \d+ -->$")


def _normalize(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold())


def _clean_line(value: str) -> str:
    value = value.replace("\uf06c", "•").replace("\uf0b7", "•")
    value = "".join(" " if 0xE000 <= ord(char) <= 0xF8FF else char for char in value)
    return re.sub(r"[ \t]+", " ", value).strip()


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
) -> tuple[dict[tuple[int, int], int], int]:
    levels: dict[tuple[int, int], int] = {}
    matched = 0
    for bookmark in bookmarks:
        if not 1 <= bookmark.page <= len(pages):
            continue
        target = _normalize(bookmark.title)
        best_index = -1
        best_score = 0.0
        for index, line in enumerate(pages[bookmark.page - 1]):
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
        if best_index >= 0 and best_score >= 0.68:
            key = (bookmark.page, best_index)
            level = min(4, 2 + bookmark.depth)
            levels[key] = min(level, levels.get(key, level))
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
    named_contents = "目录" in heading or bool(re.search(r"\bcontents\b", heading))
    chapter_entries = sum(bool(_CHAPTER_RE.match(line)) for line in nonempty)
    short_entries = sum(len(line) <= 80 for line in nonempty)
    return bool(
        named_contents
        and chapter_entries >= 2
        and len(nonempty) >= 12
        and short_entries / len(nonempty) >= 0.75
    )


def _reflow_page(
    page_number: int,
    lines: list[str],
    heading_levels: dict[tuple[int, int], int],
    repeated_margins: set[str],
    *,
    structural_headings: bool = True,
) -> tuple[list[str], int, int]:
    output = [f"<!-- page: {page_number} -->", ""]
    paragraph: list[str] = []
    heading_count = room_count = 0

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
    for index, line in enumerate(lines):
        if not line:
            flush()
            continue
        if index in margins and _normalize(line) in repeated_margins:
            continue
        if index in margins and _PAGE_NUMBER_RE.fullmatch(line):
            continue
        level = heading_levels.get((page_number, index)) if structural_headings else None
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
        if structural_headings and _CHAPTER_RE.match(line) and (
            level is not None
            or chapter_confirmation
            or (index in top_lines and chapter_lines == 1)
        ):
            level = 1
        elif structural_headings and _ROOM_RE.match(line):
            level = level or 4
            room_count += 1
        elif structural_headings and level is None and _looks_like_all_caps_heading(line):
            level = 5
        if level is not None:
            flush()
            output.extend((f"{'#' * level} {line}", ""))
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
) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    """Normalize extracted PDF pages into provenance-preserving Markdown."""
    bookmarks = bookmarks or []
    pages = [[_clean_line(line) for line in text.splitlines()] for text in page_texts]
    repeated = _repeated_margin_lines(pages)
    heading_levels, matched = _match_bookmarks(pages, bookmarks)
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
            "heading_count": heading_count,
            "room_heading_count": room_count,
            "toc_pages": sorted(toc_pages),
        },
        tuple(warnings),
    )


class MarkdownDocumentConverter:
    def convert(self, path: str | Path) -> NormalizedDocument:
        source = Path(path).expanduser().resolve()
        content = source.read_text(encoding="utf-8")
        return NormalizedDocument(
            content=content,
            media_type="text/markdown",
            source_path=str(source),
            checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
        )


class PdfDocumentConverter:
    def __init__(self, *, ocr_provider: OcrProvider | None = None) -> None:
        self.ocr_provider = ocr_provider

    def convert(self, path: str | Path) -> NormalizedDocument:
        source = Path(path).expanduser().resolve()
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError(
                "PDF conversion requires `pip install sagasmith-core[documents]`"
            ) from exc
        reader = PdfReader(str(source))
        pages = [page.extract_text() or "" for page in reader.pages]
        sparse = sum(len(re.sub(r"\s+", "", page)) < 20 for page in pages)
        if pages and sparse / len(pages) >= 0.8:
            if self.ocr_provider is None:
                raise DocumentQualityError(
                    "pdf_text_unavailable",
                    "PDF has no usable text layer; configure an OCR provider",
                )
            pages = self.ocr_provider.extract(source)
        bookmarks = self._bookmarks(reader)
        content, stats, warnings = build_structured_markdown(pages, bookmarks)
        return NormalizedDocument(
            content=content,
            media_type="application/pdf",
            source_path=str(source),
            checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
            page_count=len(pages),
            bookmarks=tuple(bookmarks),
            warnings=warnings,
            metadata={
                **stats,
                "ocr_provider": self.ocr_provider.name if self.ocr_provider else None,
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


def converter_for(path: str | Path, *, ocr_provider: OcrProvider | None = None):
    suffix = Path(path).suffix.casefold()
    if suffix == ".pdf":
        return PdfDocumentConverter(ocr_provider=ocr_provider)
    if suffix in {".md", ".markdown", ".txt"}:
        return MarkdownDocumentConverter()
    raise ValueError(f"unsupported document type: {suffix}")


def page_for_offset(content: str, offset: int) -> int | None:
    current: int | None = None
    cursor = 0
    for line in content.splitlines(keepends=True):
        if cursor > offset:
            break
        if match := re.match(r"<!-- page: (\d+) -->", line.strip()):
            current = int(match.group(1))
        cursor += len(line)
    return current


def strip_page_markers(value: str) -> str:
    return "\n".join(
        line for line in value.splitlines() if not _PAGE_MARKER_RE.match(line.strip())
    ).strip()
