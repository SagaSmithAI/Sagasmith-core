import re
from pathlib import Path


def test_core_document_parser_contains_no_dnd_semantic_vocabulary() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "sagasmith_core"
    sources = "\n".join(
        (source_root / name).read_text(encoding="utf-8")
        for name in ("documents.py", "parsing.py")
    )
    forbidden = (
        "armor class",
        "challenge rating",
        "creature type",
        "d&d",
        "dnd",
        "spell list",
        "statblock",
        "subclass",
    )

    assert not {
        token for token in forbidden if re.search(rf"(?i)\b{re.escape(token)}\b", sources)
    }
