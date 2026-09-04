import pytest

from sagasmith_core.documents import _document_quality, _revised_document_quality


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("character_count", None),
        ("character_count", True),
        ("character_count", -1),
        ("replacement_character_count", "2"),
        ("replacement_character_pages", None),
        ("replacement_character_pages", [0]),
        ("replacement_character_pages", [3]),
        ("replacement_character_pages", [True]),
        ("replacement_character_pages", [2, 2]),
    ],
)
def test_invalid_baseline_uses_complete_final_page_quality(field, invalid):
    before = {1: "A" * 100 + "\ufffd"}
    after = {1: "A" * 100 + "e", 2: "B" * 100 + "\ufffd"}
    baseline = _document_quality([before[1], after[2]])
    baseline[field] = invalid

    result = _revised_document_quality(baseline, before, after, 2)

    assert result == _document_quality(list(after.values()))
    assert result["replacement_character_pages"] == [2]


def test_missing_replacement_page_baseline_retains_unreviewed_corruption():
    before = {1: "A" * 100 + "\ufffd"}
    after = {1: "A" * 100 + "e", 2: "B" * 100 + "\ufffd"}
    baseline = _document_quality([before[1], after[2]])
    baseline.pop("replacement_character_pages")

    assert _revised_document_quality(baseline, before, after, 2) == _document_quality(
        list(after.values())
    )
