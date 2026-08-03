from pathlib import Path

import pytest

from sagasmith_core.documents import (
    PdfTextLayoutProvider,
    _layout_repairs_missing_word_spaces,
    _pdf_text_layout_blocks,
    render_pdf_page,
)


class _PositionedTextPage:
    def __init__(self, values: list[tuple[str, float, float]]) -> None:
        self.values = values

    def count_chars(self) -> int:
        return len(self.values)

    def get_text_range(self, index: int, count: int) -> str:
        assert count == 1
        return self.values[index][0]

    def get_charbox(self, index: int, *, loose: bool) -> tuple[float, ...]:
        assert loose is True
        _character, x, y = self.values[index]
        return (x, y, x + 5, y + 10)


def test_render_pdf_page_returns_provenance_preserving_png(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    pytest.importorskip("PIL")
    source = tmp_path / "map.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=100)
    with source.open("wb") as stream:
        writer.write(stream)

    rendered = render_pdf_page(source, 1, scale=1.0)

    assert rendered.media_type == "image/png"
    assert rendered.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert rendered.page_number == 1
    assert rendered.page_count == 1
    assert rendered.width == 200
    assert rendered.height == 100
    assert len(rendered.checksum) == 64


def test_render_pdf_page_rejects_an_out_of_range_page(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    pytest.importorskip("PIL")
    source = tmp_path / "one-page.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=10, height=10)
    with source.open("wb") as stream:
        writer.write(stream)

    with pytest.raises(ValueError, match="between 1 and 1"):
        render_pdf_page(source, 2)


def test_pdf_text_layout_preserves_independent_columns(tmp_path: Path) -> None:
    pytest.importorskip("reportlab")
    from reportlab.pdfgen.canvas import Canvas

    source = tmp_path / "columns.pdf"
    canvas = Canvas(str(source), pagesize=(612, 792))
    for x, name in ((45, "LEFT CREATURE"), (330, "RIGHT CREATURE")):
        canvas.drawString(x, 720, name)
        canvas.drawString(x, 704, "Medium humanoid, neutral")
        canvas.drawString(x, 688, "Armor Class 15")
        canvas.drawString(x, 672, "Hit Points 22 (4d8 + 4)")
        canvas.drawString(x, 656, "Speed 30 ft.")
    canvas.save()

    layout = PdfTextLayoutProvider().extract_layout(source, page_numbers=[1])[0]
    texts = [block.text for block in layout.blocks]

    assert texts.count("Armor Class 15") == 2
    assert "LEFT CREATURE" in texts
    assert "RIGHT CREATURE" in texts
    assert not any("LEFT CREATURE" in text and "RIGHT CREATURE" in text for text in texts)


def test_pdf_text_layout_character_grouping_separates_same_line_columns() -> None:
    values = []
    for text, start in (("LEFT CREATURE", 10.0), ("RIGHT CREATURE", 220.0)):
        values.extend(
            (character, start + index * 5.0, 70.0)
            for index, character in enumerate(text)
        )

    blocks = _pdf_text_layout_blocks(
        _PositionedTextPage(values),
        page_height=100.0,
    )

    assert [block.text for block in blocks] == ["LEFT CREATURE", "RIGHT CREATURE"]


def test_pdf_text_layout_restores_word_spaces_from_character_gaps() -> None:
    values = []
    x = 10.0
    for index, character in enumerate("CircleofWildfire"):
        values.append((character, x, 70.0))
        x += 5.0
        if index in {5, 7}:
            x += 2.0

    blocks = _pdf_text_layout_blocks(
        _PositionedTextPage(values),
        page_height=100.0,
    )

    assert [block.text for block in blocks] == ["Circle of Wildfire"]
    assert _layout_repairs_missing_word_spaces(
        "CircleofWildfire",
        "Circle of Wildfire",
    ) is True
    assert _layout_repairs_missing_word_spaces(
        "Circle of Wildfire",
        "Circle of Wildfire",
    ) is False


def test_pdf_text_layout_respects_an_explicit_empty_page_selection(
    tmp_path: Path,
) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    source = tmp_path / "one-page.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=10, height=10)
    with source.open("wb") as stream:
        writer.write(stream)

    assert PdfTextLayoutProvider().extract_layout(source, page_numbers=[]) == []
