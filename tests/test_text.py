from sagasmith_core.text import ascii_slug, compact_ascii_key


def test_ascii_text_keys_share_casefold_and_separator_contract() -> None:
    assert compact_ascii_key("  Dragon's_Lair 42  ") == "dragonslair42"
    assert ascii_slug("  Dragon's_Lair 42  ") == "dragon-s-lair-42"
    assert compact_ascii_key(None) == ""
    assert ascii_slug("神殿") == ""
