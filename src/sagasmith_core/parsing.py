"""Shared parsed-document structures and Markdown hierarchy parser."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ParsedChunk:
    ordinal: int
    heading_path: tuple[str, ...]
    content: str
    start_offset: int
    end_offset: int
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedSection:
    ordinal: int
    level: int
    title: str
    path: tuple[str, ...]
    content: str
    start_offset: int
    end_offset: int
    chunks: tuple[ParsedChunk, ...]
    metadata: dict = field(default_factory=dict)


class MarkdownHierarchyParser:
    """Parse headings and produce bounded retrieval chunks."""

    def __init__(self, *, chunk_size: int = 1800, chunk_overlap: int = 180) -> None:
        if chunk_size < 200:
            raise ValueError("chunk_size must be at least 200 characters")
        if not 0 <= chunk_overlap < chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def parse(self, content: str) -> list[ParsedSection]:
        matches = list(_HEADING.finditer(content))
        if not matches:
            return [self._section(0, 1, "Document", ("Document",), content, 0, len(content))]

        sections: list[ParsedSection] = []
        heading_stack: list[tuple[int, str]] = []
        for ordinal, match in enumerate(matches):
            level = len(match.group(1))
            title = match.group(2).strip()
            heading_stack = [item for item in heading_stack if item[0] < level]
            heading_stack.append((level, title))
            path = tuple(item[1] for item in heading_stack)
            start = match.end()
            end = matches[ordinal + 1].start() if ordinal + 1 < len(matches) else len(content)
            body = content[start:end].strip()
            sections.append(
                self._section(
                    ordinal,
                    level,
                    title,
                    path,
                    body,
                    start,
                    end,
                )
            )
        return sections

    def _section(
        self,
        ordinal: int,
        level: int,
        title: str,
        path: tuple[str, ...],
        body: str,
        start: int,
        end: int,
    ) -> ParsedSection:
        chunks = tuple(self._chunks(body, path, start))
        return ParsedSection(
            ordinal=ordinal,
            level=level,
            title=title,
            path=path,
            content=body,
            start_offset=start,
            end_offset=end,
            chunks=chunks,
        )

    def _chunks(
        self,
        content: str,
        path: tuple[str, ...],
        base_offset: int,
    ) -> list[ParsedChunk]:
        if not content:
            return [
                ParsedChunk(
                    ordinal=0,
                    heading_path=path,
                    content="",
                    start_offset=base_offset,
                    end_offset=base_offset,
                )
            ]
        chunks: list[ParsedChunk] = []
        cursor = 0
        while cursor < len(content):
            hard_end = min(len(content), cursor + self.chunk_size)
            end = hard_end
            if hard_end < len(content):
                paragraph = content.rfind("\n\n", cursor, hard_end)
                sentence = max(
                    content.rfind("。", cursor, hard_end),
                    content.rfind(". ", cursor, hard_end),
                )
                split = max(paragraph, sentence)
                if split > cursor + self.chunk_size // 2:
                    end = split + (1 if content[split] == "。" else 0)
            text = content[cursor:end].strip()
            chunks.append(
                ParsedChunk(
                    ordinal=len(chunks),
                    heading_path=path,
                    content=text,
                    start_offset=base_offset + cursor,
                    end_offset=base_offset + end,
                )
            )
            if end >= len(content):
                break
            cursor = max(cursor + 1, end - self.chunk_overlap)
        return chunks

