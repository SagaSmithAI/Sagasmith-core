from __future__ import annotations

import hashlib
import json
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, select

import sagasmith_core.snapshots as snapshot_module
from sagasmith_core import (
    FACT_KEY_WRITE_ACTIONS,
    ActorKnowledgeService,
    ActorKnowledgeTransfer,
    BranchService,
    CampaignService,
    CharacterService,
    CharacterStateUpdate,
    ContinuityCommitService,
    ContinuityService,
    EventService,
    IdempotencyService,
    IdempotencyWrite,
    MemoryService,
    ModuleService,
    RevisionService,
    RuleProfileService,
    RuleReceiptService,
    RuleService,
    SnapshotService,
    StateMutationService,
)
from sagasmith_core.access import (
    CAMPAIGN_DM_ROLES,
    CAMPAIGN_ROLES,
    LOCAL_SYSTEM_PRINCIPAL_ID,
)
from sagasmith_core.documents import (
    _PDF_TEXT_EXTRACTOR_VERSION,
    GENERIC_DOCUMENT_LAYOUT_PROFILE,
    CascadingOcrProvider,
    DocumentBookmark,
    DocumentLayoutProfile,
    NormalizedDocument,
    OcrPageLayout,
    OcrTextBlock,
    PageLocator,
    PdfDocumentConverter,
    RapidOcrProvider,
    _bookmark_ocr_candidate_pages,
    _cache_profile,
    _layout_reading_order_text,
    _looks_like_corrupt_visual_heading,
    _ocr_page_layout,
    _ocr_suspect_pages,
    _page_quality,
    _pdf_form_metadata,
    _repair_pdf_control_artifacts,
    _repair_pdf_word_break_noncharacters,
    apply_document_page_revisions,
    build_structured_markdown,
    extract_pdf_page_text,
    normalize_document,
    normalized_document_page_text,
    ocr_layout_text,
)
from sagasmith_core.idempotency import request_hash
from sagasmith_core.models import (
    ActorKnowledgeRevision,
    AuditLog,
    Campaign,
    CampaignBranch,
    CampaignEvent,
    CampaignEventParticipant,
    CampaignSnapshot,
    ModuleSource,
    MutationGroup,
    SnapshotActorKnowledgeBinding,
    StateDocument,
    StateRevision,
)
from sagasmith_core.snapshot_storage import (
    MAX_SNAPSHOT_PAYLOAD_BYTES,
    SnapshotStorageError,
    decode_snapshot_payload,
    encode_snapshot_payload,
    snapshot_record_checksum,
)
from sagasmith_core.snapshots import SnapshotIntegrityError
from sagasmith_core.state_document_storage import StateDocumentStorageError
from sagasmith_core.visibility import (
    ACTOR_KNOWLEDGE_DISCLOSURE_SCOPES,
    EVENT_AUDIENCE_SCOPES,
    MEMORY_DISCLOSURE_SCOPES,
    PLAYER_EVENT_AUDIENCE_SCOPES,
    PLAYER_MEMORY_DISCLOSURE_SCOPES,
    PLAYER_OWNED_ACTOR_DISCLOSURE_SCOPES,
)


def test_continuity_fact_key_actions_have_one_public_contract() -> None:
    assert FACT_KEY_WRITE_ACTIONS == {"add", "upsert"}


def test_visibility_vocabularies_keep_distinct_semantics_explicit() -> None:
    assert CAMPAIGN_DM_ROLES < CAMPAIGN_ROLES
    assert LOCAL_SYSTEM_PRINCIPAL_ID == "system:local"
    assert PLAYER_EVENT_AUDIENCE_SCOPES < EVENT_AUDIENCE_SCOPES
    assert PLAYER_MEMORY_DISCLOSURE_SCOPES < MEMORY_DISCLOSURE_SCOPES
    assert PLAYER_OWNED_ACTOR_DISCLOSURE_SCOPES < ACTOR_KNOWLEDGE_DISCLOSURE_SCOPES
    assert "actor" in EVENT_AUDIENCE_SCOPES
    assert "actor" not in ACTOR_KNOWLEDGE_DISCLOSURE_SCOPES
    assert "owner" in ACTOR_KNOWLEDGE_DISCLOSURE_SCOPES
    assert "owner" not in MEMORY_DISCLOSURE_SCOPES


def test_active_branch_pointer_is_the_only_current_branch_authority(database) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Branch authority",
    )
    active_branch_id = BranchService(database).current(campaign.id).id

    current = BranchService(database).current(campaign.id)
    assert current.id == active_branch_id
    assert current.is_current is True
    assert [item.is_current for item in BranchService(database).list(campaign.id)] == [True]

    with database.transaction() as session:
        campaign_row = session.get(Campaign, campaign.id)
        branch_row = session.get(CampaignBranch, active_branch_id)
        assert campaign_row is not None
        assert branch_row is not None
        assert campaign_row.active_branch_id == branch_row.id
        assert not hasattr(branch_row, "is_current")


def test_ocr_page_layout_retains_coordinates_for_text_only_recovery() -> None:
    layout = _ocr_page_layout(
        SimpleNamespace(
            boxes=[
                [[82, 180], [388, 184], [388, 218], [82, 214]],
                [[84, 212], [253, 212], [253, 238], [84, 238]],
            ],
            txts=["ADULT BLUE DRAGON", "Huge dragon, lawful evil"],
            scores=[0.99092, 0.98081],
        ),
        page_number=92,
        image_shape=(1584, 1018, 3),
    )

    assert layout.as_dict() == {
        "page_number": 92,
        "width": 1018,
        "height": 1584,
        "blocks": [
            {
                "text": "ADULT BLUE DRAGON",
                "confidence": 0.99092,
                "bbox": [82.0, 180.0, 388.0, 218.0],
            },
            {
                "text": "Huge dragon, lawful evil",
                "confidence": 0.98081,
                "bbox": [84.0, 212.0, 253.0, 238.0],
            },
        ],
    }


def test_rapidocr_profile_binds_model_version_and_scale() -> None:
    assert RapidOcrProvider().model_type == "medium"

    small = RapidOcrProvider(scale=2.0, model_type="small")
    medium = RapidOcrProvider(scale=2.0, model_type="medium")

    assert "ocr=PP-OCRv6" in small.cache_profile
    assert "model=small" in small.cache_profile
    assert "scale=2.00" in small.cache_profile
    assert small.cache_profile != medium.cache_profile

    with pytest.raises(ValueError, match="model_type"):
        RapidOcrProvider(model_type="server")


def test_rapidocr_optional_runtime_has_functional_opencv() -> None:
    cv2 = pytest.importorskip("cv2")

    assert callable(getattr(cv2, "resize", None))


def test_rapidocr_engine_is_shared_by_model_not_render_scale(monkeypatch) -> None:
    import sagasmith_core.documents as documents

    engines = {"small": object(), "medium": object()}
    created: list[str] = []

    def fake_engine(model_name: str):
        created.append(model_name)
        return engines[model_name]

    documents._RAPIDOCR_ENGINES.clear()
    monkeypatch.setattr(documents, "_new_rapidocr_engine", fake_engine)
    small_low = RapidOcrProvider(scale=1.5, model_type="small")
    small_high = RapidOcrProvider(scale=4.0, model_type="small")
    medium = RapidOcrProvider(scale=2.0, model_type="medium")

    assert small_low._load_engine() is small_high._load_engine() is engines["small"]
    assert medium._load_engine() is engines["medium"]
    assert small_low._engine_lock is small_high._engine_lock
    assert created == ["small", "medium"]
    documents._RAPIDOCR_ENGINES.clear()


def test_rapidocr_page_cache_survives_provider_restart_and_rejects_tampering(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"immutable source bytes")
    cache = tmp_path / "cache"
    calls: list[list[int]] = []

    def recovered_layouts(path, *, page_numbers=None, on_page=None):
        assert path == source.resolve()
        pages = list(page_numbers or [])
        calls.append(pages)
        layouts = [
            OcrPageLayout(
                page_number=page_number,
                width=600,
                height=800,
                blocks=(
                    OcrTextBlock(
                        f"Recovered page {page_number}",
                        0.99,
                        20,
                        20,
                        580,
                        60,
                    ),
                ),
            )
            for page_number in pages
        ]
        if on_page is not None:
            for layout in layouts:
                on_page(layout)
        return layouts

    first = RapidOcrProvider(model_type="medium", cache_dir=cache)
    monkeypatch.setattr(first, "_extract_layout_uncached", recovered_layouts)
    layouts = first.extract_layout(source, page_numbers=[2, 1, 2])

    assert calls == [[2, 1]]
    assert [layout.page_number for layout in layouts] == [2, 1, 2]
    assert first.cache_hits == 0
    assert first.cache_misses == 2

    restarted = RapidOcrProvider(model_type="medium", cache_dir=cache)
    monkeypatch.setattr(
        restarted,
        "_extract_layout_uncached",
        lambda path, *, page_numbers=None, on_page=None: pytest.fail("page should be cached"),
    )
    cached = restarted.extract_layout(source, page_numbers=[1, 2])

    assert [layout.blocks[0].text for layout in cached] == [
        "Recovered page 1",
        "Recovered page 2",
    ]
    assert restarted.cache_hits == 2
    assert restarted.cache_misses == 0

    page_one = next(cache.rglob("page-00001.json"))
    damaged = json.loads(page_one.read_text(encoding="utf-8"))
    damaged["layout"]["blocks"][0]["text"] = "tampered"
    page_one.write_text(json.dumps(damaged), encoding="utf-8")
    repaired = RapidOcrProvider(model_type="medium", cache_dir=cache)
    monkeypatch.setattr(repaired, "_extract_layout_uncached", recovered_layouts)
    result = repaired.extract_layout(source, page_numbers=[1, 2])

    assert calls == [[2, 1], [1]]
    assert [layout.blocks[0].text for layout in result] == [
        "Recovered page 1",
        "Recovered page 2",
    ]
    assert repaired.cache_hits == 1
    assert repaired.cache_misses == 1


def test_rapidocr_page_cache_commits_each_page_before_batch_failure(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"immutable source bytes")
    cache = tmp_path / "cache"

    def interrupted(path, *, page_numbers=None, on_page=None):
        del path
        assert page_numbers == [1, 2]
        first = OcrPageLayout(
            page_number=1,
            width=600,
            height=800,
            blocks=(OcrTextBlock("Recovered first page", 0.99, 20, 20, 580, 60),),
        )
        assert on_page is not None
        on_page(first)
        raise RuntimeError("model process interrupted")

    provider = RapidOcrProvider(model_type="medium", cache_dir=cache)
    monkeypatch.setattr(provider, "_extract_layout_uncached", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        provider.extract_layout(source, page_numbers=[1, 2])

    requested: list[int] = []

    def resume(path, *, page_numbers=None, on_page=None):
        del path
        requested.extend(page_numbers or [])
        second = OcrPageLayout(
            page_number=2,
            width=600,
            height=800,
            blocks=(OcrTextBlock("Recovered second page", 0.99, 20, 20, 580, 60),),
        )
        assert on_page is not None
        on_page(second)
        return [second]

    restarted = RapidOcrProvider(model_type="medium", cache_dir=cache)
    monkeypatch.setattr(restarted, "_extract_layout_uncached", resume)
    layouts = restarted.extract_layout(source, page_numbers=[1, 2])

    assert requested == [2]
    assert [layout.blocks[0].text for layout in layouts] == [
        "Recovered first page",
        "Recovered second page",
    ]
    assert restarted.cache_hits == 1
    assert restarted.cache_misses == 1


def test_ocr_cascade_uses_stronger_model_only_for_unusable_pages() -> None:
    class FakeLayoutOcr:
        def __init__(self, name: str, text: str) -> None:
            self.name = name
            self.cache_profile = f"{name}:v1"
            self.text = text
            self.calls: list[list[int]] = []

        def extract_layout(self, path, *, page_numbers=None):
            del path
            pages = list(page_numbers or [])
            self.calls.append(pages)
            return [
                OcrPageLayout(
                    page_number=page,
                    width=600,
                    height=800,
                    blocks=(OcrTextBlock(self.text, 0.99, 20, 20, 580, 60),) if self.text else (),
                )
                for page in pages
            ]

    small = FakeLayoutOcr("small", "")
    medium = FakeLayoutOcr(
        "medium",
        "RECOVERED HEADING\nRecovered body text for indexing.",
    )
    cascade = CascadingOcrProvider(small, medium)

    layouts = cascade.extract_layout("unused.pdf", page_numbers=[3])

    assert small.calls == [[3]]
    assert medium.calls == [[3]]
    assert layouts[0].blocks[0].text.startswith("RECOVERED HEADING")
    assert cascade.cache_profile == ("rapidocr-cascade:min-confidence=0.860:small:v1=>medium:v1")


def test_ocr_cascade_prefers_confidence_over_extra_garbage_characters() -> None:
    class FakeLayoutOcr:
        def __init__(self, name: str, text: str, confidence: float) -> None:
            self.name = name
            self.text = text
            self.confidence = confidence

        def extract_layout(self, path, *, page_numbers=None):
            del path
            return [
                OcrPageLayout(
                    page_number=page,
                    width=600,
                    height=800,
                    blocks=(OcrTextBlock(self.text, self.confidence, 20, 20, 580, 60),),
                )
                for page in list(page_numbers or [])
            ]

    noisy = FakeLayoutOcr("small", "ABCDEFGHIJKLMNO1234567", 0.7)
    accurate = FakeLayoutOcr("medium", "CORRECT TEXT EVIDENCE", 0.99)

    layouts = CascadingOcrProvider(noisy, accurate).extract_layout("unused.pdf", page_numbers=[3])

    assert layouts[0].blocks[0].text == "CORRECT TEXT EVIDENCE"


def test_ocr_cascade_audits_long_low_confidence_pages() -> None:
    class FakeLayoutOcr:
        def __init__(self, name: str, text: str, confidence: float) -> None:
            self.name = name
            self.text = text
            self.confidence = confidence
            self.calls: list[list[int]] = []

        def extract_layout(self, path, *, page_numbers=None):
            del path
            pages = list(page_numbers or [])
            self.calls.append(pages)
            return [
                OcrPageLayout(
                    page_number=page,
                    width=600,
                    height=800,
                    blocks=(OcrTextBlock(self.text, self.confidence, 20, 20, 580, 60),),
                )
                for page in pages
            ]

    plausible_but_uncertain = FakeLayoutOcr(
        "rapid",
        "A full paragraph whose glyph errors are not visible to length checks alone.",
        0.72,
    )
    reviewed_structure = FakeLayoutOcr(
        "structure",
        "A full paragraph whose glyph errors are corrected by stronger OCR.",
        0.98,
    )

    layouts = CascadingOcrProvider(
        plausible_but_uncertain,
        reviewed_structure,
    ).extract_layout("unused.pdf", page_numbers=[12])

    assert plausible_but_uncertain.calls == [[12]]
    assert reviewed_structure.calls == [[12]]
    assert layouts[0].blocks[0].text.endswith("stronger OCR.")


def test_ocr_cascade_rejects_high_confidence_truncated_fallback() -> None:
    class FakeLayoutOcr:
        def __init__(self, name: str, text: str, confidence: float) -> None:
            self.name = name
            self.text = text
            self.confidence = confidence

        def extract_layout(self, path, *, page_numbers=None):
            del path
            return [
                OcrPageLayout(
                    page_number=page,
                    width=600,
                    height=800,
                    blocks=(OcrTextBlock(self.text, self.confidence, 20, 20, 580, 60),),
                )
                for page in list(page_numbers or [])
            ]

    complete = FakeLayoutOcr(
        "primary",
        "The complete page remains available even though its confidence is low. " * 20,
        0.72,
    )
    truncated = FakeLayoutOcr("fallback", "A short clean heading.", 0.99)

    layouts = CascadingOcrProvider(complete, truncated).extract_layout(
        "unused.pdf", page_numbers=[7]
    )

    assert layouts[0].blocks[0].text.startswith("The complete page remains")


def test_layout_reading_order_keeps_columns_contiguous() -> None:
    layout = OcrPageLayout(
        page_number=12,
        width=600,
        height=800,
        blocks=(
            OcrTextBlock("Chapter title", 1.0, 80, 20, 520, 50),
            OcrTextBlock("left one", 1.0, 40, 90, 250, 110),
            OcrTextBlock("right one", 1.0, 340, 90, 550, 110),
            OcrTextBlock("left two", 1.0, 40, 120, 250, 140),
            OcrTextBlock("right two", 1.0, 340, 120, 550, 140),
            OcrTextBlock("left three", 1.0, 40, 150, 250, 170),
            OcrTextBlock("right three", 1.0, 340, 150, 550, 170),
            OcrTextBlock("left four", 1.0, 40, 180, 250, 200),
            OcrTextBlock("right four", 1.0, 340, 180, 550, 200),
        ),
    )

    text, used_columns = _layout_reading_order_text(layout)

    assert used_columns is True
    assert text.splitlines() == [
        "Chapter title",
        "left one",
        "left two",
        "left three",
        "left four",
        "right one",
        "right two",
        "right three",
        "right four",
    ]
    assert ocr_layout_text(layout) == (text, used_columns)


def test_layout_reading_order_uses_offset_crop_box_text_extent() -> None:
    blocks = []
    for row, y in enumerate((90, 120, 150, 180), 1):
        blocks.extend(
            (
                OcrTextBlock(f"left {row}", 1.0, 53, y, 279, y + 20),
                OcrTextBlock(f"right {row}", 1.0, 303, y, 529, y + 20),
            )
        )
    # The PDF crop box is 538 points wide, but positioned glyphs retain an
    # approximately 36-point media-box offset.  A page-width midpoint (269)
    # therefore cuts through the left column instead of through the real
    # 279-303 gutter.
    layout = OcrPageLayout(
        page_number=150,
        width=538,
        height=800,
        blocks=tuple(blocks),
    )

    text, used_columns = _layout_reading_order_text(layout)

    assert used_columns is True
    assert text.splitlines() == [
        "left 1",
        "left 2",
        "left 3",
        "left 4",
        "right 1",
        "right 2",
        "right 3",
        "right 4",
    ]


def test_layout_reading_order_keeps_three_columns_contiguous() -> None:
    blocks = [OcrTextBlock("Chapter title", 1.0, 80, 20, 520, 50)]
    for row, y in enumerate((90, 120, 150, 180), 1):
        blocks.extend(
            (
                OcrTextBlock(f"left {row}", 1.0, 30, y, 170, y + 20),
                OcrTextBlock(f"middle {row}", 1.0, 230, y, 370, y + 20),
                OcrTextBlock(f"right {row}", 1.0, 430, y, 570, y + 20),
            )
        )
    layout = OcrPageLayout(
        page_number=13,
        width=600,
        height=800,
        blocks=tuple(blocks),
    )

    text, used_columns = _layout_reading_order_text(layout)

    assert used_columns is True
    assert text.splitlines() == [
        "Chapter title",
        "left 1",
        "left 2",
        "left 3",
        "left 4",
        "middle 1",
        "middle 2",
        "middle 3",
        "middle 4",
        "right 1",
        "right 2",
        "right 3",
        "right 4",
    ]


def test_layout_reading_order_keeps_four_column_catalogs_contiguous() -> None:
    blocks = []
    for column, x0 in enumerate((20.0, 120.0, 220.0, 320.0), start=1):
        for row in range(5):
            blocks.append(
                OcrTextBlock(
                    f"column {column} row {row + 1}",
                    1.0,
                    x0,
                    20.0 + row * 15.0,
                    x0 + 65.0,
                    30.0 + row * 15.0,
                )
            )
    layout = OcrPageLayout(
        page_number=14,
        width=400.0,
        height=120.0,
        blocks=tuple(reversed(blocks)),
    )

    text, used_columns = _layout_reading_order_text(layout)

    assert used_columns is True
    assert text.index("column 1 row 5") < text.index("column 2 row 1")
    assert text.index("column 2 row 5") < text.index("column 3 row 1")
    assert text.index("column 3 row 5") < text.index("column 4 row 1")


def test_rule_document_path_ingest_preserves_source_and_page_provenance(database, tmp_path) -> None:
    path = tmp_path / "optional-rules.md"
    path.write_text("# Options\n## Tool Synergy\nUse both proficiencies.\n", encoding="utf-8")
    rules = RuleService(database)

    inspection = rules.inspect_path(path)
    assert inspection["sections"] == 2
    assert inspection["checksum"]
    result = rules.ingest_path(
        system_id="dnd5e",
        path=path,
        source_key="optional-rules",
        title="Optional Rules",
        edition="2014",
        publication_id="optional",
    )
    hit = rules.search(system_id="dnd5e", query="Tool Synergy", top_k=1)[0]
    citation = rules.citation(hit.id, source_id=result.source_id)
    expanded = rules.expand(hit.id)
    with pytest.raises(ValueError, match="does not belong"):
        rules.citation(hit.id, source_id="another-source")

    assert hit.metadata["source_checksum"] == inspection["checksum"]
    assert citation["source"] == "rule-source:optional-rules"
    assert citation["source_checksum"] == inspection["checksum"]
    assert expanded["source"]["metadata"]["source_path"] == str(path.resolve())

    path.write_text("# Options\n## Tool Synergy Revised\nNew procedure.\n", encoding="utf-8")
    replaced = rules.ingest_path(
        system_id="dnd5e",
        path=path,
        source_key="optional-rules",
        title="Optional Rules",
        edition="2014",
        publication_id="optional",
    )
    assert replaced.source_id != result.source_id
    revised = rules.search(system_id="dnd5e", query="New procedure", top_k=1)[0]
    assert revised.source_id == replaced.source_id
    assert "New procedure" in revised.content

    paged = NormalizedDocument(
        content=(
            "<!-- page: 7 -->\n# Options\n## Tool Synergy\nUse both proficiencies.\n"
            "<!-- page: 8 -->\nMore guidance.\n"
        ),
        media_type="application/pdf",
        source_path=str(path.resolve()),
        checksum="source-pdf-checksum",
        page_count=8,
    )
    result = rules.ingest(
        system_id="dnd5e",
        source_key="paged-rules",
        title="Paged Rules",
        content=paged.content,
        edition="2014",
        normalized_document=paged,
    )
    hit = rules.search(system_id="dnd5e", query="More guidance", top_k=1)[0]
    citation = rules.citation(hit.id, source_id=result.source_id)
    assert citation["source_checksum"] == "source-pdf-checksum"
    assert citation["page_start"] == 7
    assert citation["page_end"] in {7, 8}


def test_normalized_document_cache_is_content_addressed(tmp_path) -> None:
    source = tmp_path / "rules.md"
    source.write_text("# Rules\n\n## Ready\n\nCached once.\n", encoding="utf-8")
    cache = tmp_path / "cache"

    first = normalize_document(source, cache_dir=cache)
    second = normalize_document(source, cache_dir=cache, expected_checksum=first.checksum)

    assert first.metadata["normalization_cache_hit"] is False
    assert second.metadata["normalization_cache_hit"] is True
    assert second.content == first.content
    assert len(list(cache.rglob("*.json"))) == 1


def test_normalized_pdf_cache_profile_binds_page_extractor_version() -> None:
    profile = _cache_profile(
        Path("rules.pdf"),
        None,
        GENERIC_DOCUMENT_LAYOUT_PROFILE,
    )

    assert f"pypdfium2:{_PDF_TEXT_EXTRACTOR_VERSION}" in profile
    assert f"layout={GENERIC_DOCUMENT_LAYOUT_PROFILE.cache_identity}" in profile


def test_layout_profile_preserves_repeated_mechanical_margin_fields() -> None:
    profile = DocumentLayoutProfile(
        name="mechanical",
        repeated_margin_exclusion_patterns=(r"(?i)^casting time\s*:",),
    )
    pages = [
        "Spell One\n1st-level evocation\nCasting Time: 1 action\nFirst effect.",
        "Spell Two\n2nd-level evocation\nCasting Time: 1 action\nSecond effect.",
    ]

    content, _metadata, _warnings = build_structured_markdown(
        pages,
        layout_profile=profile,
    )

    assert content.count("Casting Time: 1 action") == 2


def test_page_locator_reuses_one_marker_index() -> None:
    content = "<!-- page: 1 -->\nfirst\n<!-- page: 2 -->\nsecond\n<!-- page: 20 -->\nlast\n"
    locator = PageLocator(content)

    assert locator.page_for_offset(content.index("first")) == 1
    assert locator.page_for_offset(content.index("second")) == 2
    assert locator.page_for_offset(len(content)) == 20


def test_pdf_converter_ocr_replaces_only_suspect_pages(tmp_path) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    source = tmp_path / "image-only.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=100)
    with source.open("wb") as stream:
        writer.write(stream)

    class FakeOcr:
        name = "fake"

        def __init__(self) -> None:
            self.pages = []

        def extract(self, path, *, page_numbers=None):
            self.pages = list(page_numbers or [])
            return ["RECOVERED HEADING\nRecovered body text for indexing."]

    provider = FakeOcr()
    document = PdfDocumentConverter(ocr_provider=provider).convert(source)

    assert provider.pages == [1]
    assert "RECOVERED HEADING" in document.content
    assert document.metadata["ocr_pages"] == [1]
    assert document.metadata["quality"]["suspect_page_count"] == 0


def test_bounded_ocr_rejects_provider_without_page_selection(tmp_path) -> None:
    class AllPagesOnlyOcr:
        name = "all-pages-only"

        def extract(self, path):
            return ["unbounded OCR result"]

    with pytest.raises(TypeError):
        _ocr_suspect_pages(
            AllPagesOnlyOcr(),
            tmp_path / "source.pdf",
            ["embedded text"],
            [1],
        )


def test_pdf_converter_ocr_repairs_unmatched_text_bearing_bookmark(tmp_path, monkeypatch) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    source = tmp_path / "damaged-heading.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.add_outline_item("Ch. 5: Treasures", 0)
    with source.open("wb") as stream:
        writer.write(stream)
    monkeypatch.setattr(
        "sagasmith_core.documents._extract_pdfium_pages",
        lambda _source, _profile: (
            ["AGIC PLAYS A VITAL ROLE IN DAILY LIFE.\nOrdinary body text."],
            {},
            [],
        ),
    )

    class FakeOcr:
        name = "fake"

        def __init__(self) -> None:
            self.pages = []

        def extract(self, path, *, page_numbers=None):
            self.pages = list(page_numbers or [])
            return ["CHAPTER 5: TREASURES\nRecovered body text."]

    provider = FakeOcr()
    document = PdfDocumentConverter(ocr_provider=provider).convert(source)

    assert provider.pages == [1]
    assert document.metadata["bookmark_ocr_pages"] == [1]
    assert document.metadata["ocr_pages"] == [1]
    assert document.metadata["matched_bookmarks"] == 1
    assert not any("bookmark match rate" in item for item in document.warnings)


def test_pdf_converter_rejects_bookmark_ocr_that_does_not_improve_heading(
    tmp_path, monkeypatch
) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    source = tmp_path / "good-layout.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.add_outline_item("Ch. 5: Treasures", 0)
    with source.open("wb") as stream:
        writer.write(stream)
    embedded = "LEFT COLUMN\nComplete embedded body text.\nRIGHT COLUMN\nMore source text."
    monkeypatch.setattr(
        "sagasmith_core.documents._extract_pdfium_pages",
        lambda _source, _profile: ([embedded], {}, [1]),
    )

    class WorseOcr:
        name = "worse"

        def extract(self, path, *, page_numbers=None):
            del path
            assert page_numbers == [1]
            return ["BROKEN COLUMN ORDER\nIncomplete OCR text."]

    document = PdfDocumentConverter(ocr_provider=WorseOcr()).convert(source)

    assert "Complete embedded body text" in document.content
    assert "Incomplete OCR text" not in document.content
    assert document.metadata["bookmark_ocr_pages"] == []
    assert document.metadata["ocr_pages"] == []
    assert document.metadata["ocr_rejected_pages"] == []


def test_pdf_converter_preserves_layout_order_over_bookmark_only_ocr_gain(
    tmp_path, monkeypatch
) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    source = tmp_path / "three-column-spell-list.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=300, height=100)
    writer.add_outline_item("Spells", 0)
    with source.open("wb") as stream:
        writer.write(stream)
    embedded = (
        "BARD SPELLS\nFirst bard spell.\n"
        "CLERIC SPELLS\nFirst cleric spell.\n"
        "DRUID SPELLS\nFirst druid spell."
    )
    monkeypatch.setattr(
        "sagasmith_core.documents._extract_pdfium_pages",
        lambda _source, _profile: ([embedded], {}, [1]),
    )

    class BookmarkOnlyGainOcr:
        name = "bookmark-only-gain"

        def extract(self, path, *, page_numbers=None):
            del path
            assert page_numbers == [1]
            return [
                "SPELLS\nBARD SPELLS\nCLERIC SPELLS\nDRUID SPELLS\n"
                "First bard spell.\nFirst cleric spell.\nFirst druid spell."
            ]

    document = PdfDocumentConverter(ocr_provider=BookmarkOnlyGainOcr()).convert(source)

    assert document.content.index("First bard spell") < document.content.index("CLERIC SPELLS")
    assert document.metadata["bookmark_ocr_pages"] == []
    assert document.metadata["ocr_pages"] == []
    assert document.metadata["ocr_rejected_pages"] == []


def test_bookmark_ocr_candidates_skip_dense_and_layout_ordered_pages() -> None:
    bookmarks = [
        DocumentBookmark("Monk", 1, 1),
        DocumentBookmark("Spell Lists", 2, 1),
        DocumentBookmark("Treasures", 3, 1),
    ]
    pages = [
        "Dense source prose without the display title. " * 80,
        "BARD SPELLS\nCLERIC SPELLS\nDRUID SPELLS",
        "Damaged title\nShort body.",
    ]

    assert _bookmark_ocr_candidate_pages(pages, bookmarks, [2]) == [3]


def test_page_quality_detects_fused_pdf_word_streams() -> None:
    fused = (
        "Beginningatsecondlevelageometerisabletounderstandtheirarcanepower"
        "asatopographicalmapanddrawstraightlineconnectionsbetweenspells "
    ) * 6
    ordinary = (
        "Beginning at second level, a geometer can understand arcane power "
        "as a topographical map and draw connections between spells. "
    ) * 6

    assert _page_quality(fused)["fused_text"] is True
    assert _page_quality(ordinary)["fused_text"] is False


def test_pdf_word_break_noncharacter_repair_joins_layout_split_words() -> None:
    pages, count = _repair_pdf_word_break_noncharacters(
        ["The creature is trans￾\nformed.", "No sentinel here."]
    )

    assert pages == ["The creature is transformed.", "No sentinel here."]
    assert count == 1


def test_pdf_control_artifact_repair_removes_font_position_marker() -> None:
    pages, count = _repair_pdf_control_artifacts(["A glyph\x02 marker.", "Clean."])

    assert pages == ["A glyph marker.", "Clean."]
    assert count == 1


def test_page_quality_detects_ascii_font_map_lexical_damage() -> None:
    damaged = "\n".join(
        (
            "-he r od loses a leading glyph while otherwise ordinary source prose "
            "continues with enough words to look superficially complete. " * 3
        )
        for _index in range(10)
    )
    ordinary = (
        "The rod retains every leading glyph while ordinary source prose "
        "continues with enough words to be complete. "
    ) * 30

    damaged_quality = _page_quality(damaged)
    assert damaged_quality["lexically_damaged"] is True
    assert damaged_quality["damaged_ascii_line_start_count"] == 10
    assert damaged_quality["isolated_ascii_fragment_count"] >= 20
    assert _page_quality(ordinary)["lexically_damaged"] is False


def test_agent_page_revision_is_bounded_audited_and_non_destructive() -> None:
    original = NormalizedDocument(
        content=(
            "<!-- page: 1 -->\n\n# Rod Of Rulership\n\nRod, rare.\n"
            "<!-- page: 2 -->\n\n# Untouched\n\nSecond page.\n"
        ),
        media_type="application/pdf",
        source_path="rules.pdf",
        checksum="a" * 64,
        page_count=2,
    )
    page_text = normalized_document_page_text(original, 1)
    revision = {
        "source_checksum": original.checksum,
        "page_number": 1,
        "base_text_sha256": hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
        "replacements": [{"old": "Rod Of Rulership", "new": "ROD OF RULERSHIP"}],
        "reviewer": "agent:ocr-editor",
        "review_method": "agent",
        "rationale": "The alternate OCR and adjacent item headings agree on the title.",
        "evidence": {
            "primary_ocr_sha256": "b" * 64,
            "alternate_ocr_sha256": "c" * 64,
        },
    }

    revised = apply_document_page_revisions(original, [revision])

    assert "# Rod Of Rulership" in original.content
    assert "# ROD OF RULERSHIP" in revised.content
    assert normalized_document_page_text(revised, 2) == normalized_document_page_text(original, 2)
    assert revised.metadata["text_revision_pages"] == [1]
    assert revised.metadata["text_revisions"][0]["replacements"] == revision["replacements"]


def test_agent_page_revision_rejects_ambiguous_or_stale_source_text() -> None:
    document = NormalizedDocument(
        content="<!-- page: 1 -->\n\nrod and rod\n",
        media_type="application/pdf",
        source_path="rules.pdf",
        checksum="a" * 64,
        page_count=1,
    )
    page_text = normalized_document_page_text(document, 1)
    revision = {
        "source_checksum": document.checksum,
        "page_number": 1,
        "base_text_sha256": hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
        "replacements": [{"old": "rod", "new": "ROD"}],
        "reviewer": "agent:ocr-editor",
        "review_method": "agent",
        "rationale": "Attempted ambiguous repair.",
        "evidence": {"primary_ocr_sha256": "b" * 64},
    }

    with pytest.raises(ValueError, match="match exactly once"):
        apply_document_page_revisions(document, [revision])
    with pytest.raises(ValueError, match="base text checksum"):
        apply_document_page_revisions(
            document,
            [{**revision, "base_text_sha256": "0" * 64}],
        )
    with pytest.raises(ValueError, match="cannot create a physical page marker"):
        apply_document_page_revisions(
            document,
            [
                {
                    **revision,
                    "replacements": [{"old": "rod and rod", "new": "<!-- page: 2 -->"}],
                }
            ],
        )


def test_agent_page_revision_can_refine_the_same_unpublished_page() -> None:
    document = NormalizedDocument(
        content="<!-- page: 1 -->\n\n## Rod Of Rulership\n\nRod, rare.\n",
        media_type="application/pdf",
        source_path="rules.pdf",
        checksum="a" * 64,
        page_count=1,
    )
    first_page = normalized_document_page_text(document, 1)
    first = {
        "source_checksum": document.checksum,
        "page_number": 1,
        "base_text_sha256": hashlib.sha256(first_page.encode("utf-8")).hexdigest(),
        "replacements": [{"old": "## Rod Of Rulership", "new": "## ROD OF RULERSHIP"}],
        "reviewer": "agent:ocr-editor",
        "review_method": "agent",
        "rationale": "Restore title capitalization.",
        "evidence": {"basis": "agent_context"},
    }
    after_first = apply_document_page_revisions(document, [first])
    second_page = normalized_document_page_text(after_first, 1)
    second = {
        **first,
        "base_text_sha256": hashlib.sha256(second_page.encode("utf-8")).hexdigest(),
        "replacements": [{"old": "## ROD OF RULERSHIP", "new": "##### ROD OF RULERSHIP"}],
        "rationale": "Match the adjacent item-heading level.",
    }

    revised = apply_document_page_revisions(document, [first, second])

    assert "##### ROD OF RULERSHIP" in revised.content
    assert revised.metadata["text_revision_pages"] == [1]
    assert revised.metadata["text_revision_count"] == 2


def test_agent_page_revision_can_recover_empty_page_from_rendered_evidence() -> None:
    document = NormalizedDocument(
        content="<!-- page: 1 -->\n\n<!-- page: 2 -->\n\n# Existing\n",
        media_type="application/pdf",
        source_path="rules.pdf",
        checksum="a" * 64,
        page_count=2,
    )
    empty_page = normalized_document_page_text(document, 1)
    revision = {
        "source_checksum": document.checksum,
        "page_number": 1,
        "base_text_sha256": hashlib.sha256(empty_page.encode("utf-8")).hexdigest(),
        "replacements": [
            {
                "old": "",
                "new": "\n\n# CLASS FEATURES\n\nRecovered from the reviewed page image.\n\n",
            }
        ],
        "reviewer": "agent:ocr-editor",
        "review_method": "agent",
        "rationale": "The text extractor and OCR omitted the complete rendered page.",
        "evidence": {
            "basis": "rendered_page",
            "rendered_image_checksum": "b" * 64,
        },
    }

    revised = apply_document_page_revisions(document, [revision])

    assert "# CLASS FEATURES" in normalized_document_page_text(revised, 1)
    assert normalized_document_page_text(revised, 2) == "\n\n# Existing\n"

    with pytest.raises(ValueError, match="requires rendered_page evidence"):
        apply_document_page_revisions(
            document,
            [{**revision, "evidence": {"basis": "agent_context"}}],
        )


def test_agent_page_revision_rejects_empty_anchor_on_nonempty_page() -> None:
    document = NormalizedDocument(
        content="<!-- page: 1 -->\n\n# Existing\n",
        media_type="application/pdf",
        source_path="rules.pdf",
        checksum="a" * 64,
        page_count=1,
    )
    page_text = normalized_document_page_text(document, 1)
    revision = {
        "source_checksum": document.checksum,
        "page_number": 1,
        "base_text_sha256": hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
        "replacements": [{"old": "", "new": "Recovered text"}],
        "reviewer": "agent:ocr-editor",
        "review_method": "agent",
        "rationale": "Attempted insertion into a page that already has text.",
        "evidence": {"basis": "rendered_page"},
    }

    with pytest.raises(ValueError, match="is invalid"):
        apply_document_page_revisions(document, [revision])


def test_pdf_converter_routes_ascii_lexical_damage_through_ocr(
    tmp_path,
    monkeypatch,
) -> None:
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "damaged-text.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.add_blank_page(width=200, height=100)
    with source.open("wb") as stream:
        writer.write(stream)
    damaged = "\n".join(
        (
            "-he r od loses a leading glyph while otherwise ordinary source prose "
            "continues with enough words to look superficially complete. " * 3
        )
        for _index in range(10)
    )
    ordinary = ("The second page retains every glyph and supplies ordinary source text. ") * 30
    monkeypatch.setattr(
        "sagasmith_core.documents._extract_pdfium_pages",
        lambda _source, _profile: ([damaged, ordinary], {}, [1, 2]),
    )

    class CorrectingOcr:
        name = "correcting-ocr"

        def extract(self, path, *, page_numbers=None):
            del path
            assert page_numbers == [1]
            return [
                "ROD OF RULERSHIP\nRod, rare\n"
                + "The rod retains every leading glyph in the recovered text. " * 50
            ]

    document = PdfDocumentConverter(ocr_provider=CorrectingOcr()).convert(source)

    assert document.metadata["initial_quality"]["lexical_damage_pages"] == [1]
    assert document.metadata["ocr_pages"] == [1]
    assert document.metadata["quality"]["lexical_damage_pages"] == []
    assert "ROD OF RULERSHIP" in document.content


def test_pdf_converter_rejects_truncated_lexical_damage_ocr(
    tmp_path,
    monkeypatch,
) -> None:
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "damaged-text.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.add_blank_page(width=200, height=100)
    with source.open("wb") as stream:
        writer.write(stream)
    damaged = "\n".join(
        (
            "-he r od loses a leading glyph while otherwise ordinary source prose "
            "continues with enough words to look superficially complete. " * 3
        )
        for _index in range(10)
    )
    ordinary = ("The second page retains every glyph and supplies ordinary source text. ") * 30
    monkeypatch.setattr(
        "sagasmith_core.documents._extract_pdfium_pages",
        lambda _source, _profile: ([damaged, ordinary], {}, [1, 2]),
    )

    class TruncatingOcr:
        name = "truncating-ocr"

        def extract(self, path, *, page_numbers=None):
            del path
            assert page_numbers == [1]
            return ["ROD OF RULERSHIP\nRod, rare"]

    document = PdfDocumentConverter(ocr_provider=TruncatingOcr()).convert(source)

    assert document.metadata["ocr_pages"] == []
    assert document.metadata["ocr_rejected_pages"] == [1]
    assert "-he r od loses a leading glyph" in document.content
    assert document.metadata["quality"]["lexical_damage_pages"] == [1]


def test_extract_pdf_page_text_validates_the_physical_page(tmp_path) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    source = tmp_path / "two-pages.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.add_blank_page(width=200, height=100)
    with source.open("wb") as stream:
        writer.write(stream)

    assert extract_pdf_page_text(source, 2) == ""
    with pytest.raises(ValueError, match="outside the PDF"):
        extract_pdf_page_text(source, 3)


def test_pdf_page_extraction_cache_survives_normalizer_cache_refresh(tmp_path) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    source = tmp_path / "image-only.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=100)
    with source.open("wb") as stream:
        writer.write(stream)

    class CountingOcr:
        name = "counting"

        def __init__(self) -> None:
            self.calls = 0

        def extract(self, path, *, page_numbers=None):
            self.calls += 1
            return ["RECOVERED HEADING\nRecovered body text for indexing."]

    provider = CountingOcr()
    cache = tmp_path / "cache"
    first = normalize_document(source, ocr_provider=provider, cache_dir=cache)
    for path in cache.glob("*/*.json"):
        path.unlink()
    second = normalize_document(source, ocr_provider=provider, cache_dir=cache)

    assert first.metadata["extraction_cache_hit"] is False
    assert second.metadata["extraction_cache_hit"] is True
    assert provider.calls == 1


def test_pdf_normalization_recovers_unbookmarked_all_caps_subheadings() -> None:
    content, metadata, warnings = build_structured_markdown(
        ["TOOL PROFICIENCIES\nIntro.\nTOOLS AND SKILLS TOGETHER\nOptional procedure."],
        [DocumentBookmark("Tool Proficiencies", 1, 2)],
    )

    assert "#### TOOL PROFICIENCIES" in content
    assert "##### TOOLS AND SKILLS TOGETHER" in content
    assert metadata["heading_count"] == 2
    assert warnings == ()


def test_pdf_normalization_recovers_bounded_display_ocr_damage_and_plus_heading() -> None:
    content, metadata, warnings = build_structured_markdown(
        [
            "ADAMANTINE ARMOR\n"
            "Armor (heavy), uncommon\n"
            "ALCHEMY jUG\n"
            "Wondrous item, uncommon\n"
            "AMMUNITION, +1, +2, OR +3\n"
            "Weapon (any ammunition), uncommon"
        ],
        [],
    )

    assert "##### ALCHEMY jUG" in content
    assert "##### AMMUNITION, +1, +2, OR +3" in content
    assert metadata["heading_count"] == 3
    assert warnings == ()


@pytest.mark.parametrize(
    "value",
    [
        r"Mi r~eS t\re 5°Anc{Arc4",
        r"SACY-ec\ f \Aces thAt We know",
        "Wh'f f\"i tor one ju'tqe When tu CAn hA'v'e",
    ],
)
def test_pdf_visual_heading_rejects_decorative_glyph_noise(value: str) -> None:
    assert _looks_like_corrupt_visual_heading(value) is True


def test_pdf_normalization_sanitizes_leader_and_demotes_corrupt_hint() -> None:
    content, metadata, warnings = build_structured_markdown(
        ["CHARACTER ADVANCEMENT ~~~~~~~~~~\nP,\\RT 3: THE SPIDER'S WEB\nBody."],
        [],
        {
            1: [
                ("CHARACTER ADVANCEMENT ~~~~~~~~~~", 4),
                ("P,\\RT 3: THE SPIDER'S WEB", 4),
            ]
        },
    )

    assert "#### CHARACTER ADVANCEMENT" in content
    assert "#### P,\\RT" not in content
    assert metadata["heading_count"] == 1
    assert warnings == ()


def test_pdf_normalization_recovers_letter_spaced_unbookmarked_subheading() -> None:
    content, metadata, warnings = build_structured_markdown(
        [
            'The only exception is "Seek the Keep," which should be first.\n'
            "Se e k t h e K eep\n"
            "Characters have random encounters with raiders.\n"
            "T he town remains under attack."
        ],
        [],
    )

    assert "##### Seek the Keep\n\nCharacters have random encounters" in content
    assert "##### T he town" not in content
    assert metadata["heading_count"] == 1
    assert warnings == ()


def test_pdf_normalization_drops_split_suffix_of_previous_heading() -> None:
    content, metadata, warnings = build_structured_markdown(
        [
            "Planned Road Events\n"
            "E v e n t s\n"
            "After the travelers join, three planned events must take place."
        ],
        [],
        {1: [("Planned Road Events", 3), ("E v e n t s", 4)]},
    )

    assert "### Planned Road Events" in content
    assert "#### events" not in content
    assert metadata["heading_count"] == 1
    assert warnings == ()


def test_pdf_normalization_recovers_room_codes_with_ocr_one_before_digit() -> None:
    content, metadata, warnings = build_structured_markdown(
        [
            "XlO. NOSKA'S QUARTERS\n"
            "A rust monster waits in a cage.\n"
            "Xll. AHMAERGO'S COLLECTION\n"
            "A stuffed minotaur stands here.\n"
            "Xl3. THORVIN'S WORKSHOP\n"
            "Thorvin is building a contraption.\n"
            "Xl7. PROMENADE\n"
            "Pillars carved with eyes follow the hall.\n"
            "Xl9. XANATHAR'S SANCTUM\n"
            "A fishbowl dominates the room."
        ],
        [],
    )

    assert "#### X10. NOSKA'S QUARTERS" in content
    assert "#### X11. AHMAERGO'S COLLECTION" in content
    assert "#### X13. THORVIN'S WORKSHOP" in content
    assert "#### X17. PROMENADE" in content
    assert "#### X19. XANATHAR'S SANCTUM" in content
    assert metadata["room_heading_count"] == 5
    assert warnings == ()


def test_pdf_normalization_splits_inline_room_prose_from_heading() -> None:
    content, metadata, warnings = build_structured_markdown(
        [
            "CG2. Storage. Guests can store their traveling gear here.\n"
            "B9a. Stone stairs climb 10 feet to a wooden door.\n"
            "DAY1. The first day's travel is by foot through tangled brush."
        ],
        [],
    )

    assert "#### CG2. Storage\n\nGuests can store" in content
    assert "#### B9a\n\nStone stairs climb" in content
    assert "#### DAY1\n\nThe first day's travel" in content
    assert metadata["room_heading_count"] == 3
    assert warnings == ()


def test_pdf_normalization_rejects_wrapped_prose_as_room_codes() -> None:
    content, metadata, warnings = build_structured_markdown(
        [
            "FA11. The dragon doesn't target the adventurers at first,\n"
            "and every breath attack kills defenders.\n"
            "D6. On a roll of 1, an encounter occurs. Then roll on the table.\n"
            "Map 5.1 shows this level of the dungeon.\n"
            "TA11. Melee Weapon Attack: +8 to hit, reach 20 ft., one target."
        ],
        [],
    )

    assert "#### FA11." not in content
    assert "#### D6." not in content
    assert "#### Map 5.1" not in content
    assert "#### TA11." not in content
    assert metadata["room_heading_count"] == 0
    assert warnings == ("no structural headings were recovered",)


def test_pdf_normalization_does_not_treat_alpha_abbreviation_as_ocr_room_code() -> None:
    content, metadata, warnings = build_structured_markdown(
        [
            "FOO. This is ordinary prose without a numbered room code.\n"
            "BOW1. (The fish keeper uses the pallet as a bed.)"
        ],
        [],
    )

    assert "#### FOO." not in content
    assert "#### BOW1." not in content
    assert metadata["room_heading_count"] == 0
    assert warnings == ("no structural headings were recovered",)


def test_pdf_normalization_uses_visual_heading_hints_for_mixed_case_titles() -> None:
    content, metadata, warnings = build_structured_markdown(
        [
            "Spell Descriptions\nFireball\n3rd-level evocation\n"
            "Casting Time: 1 action\nRange: 150 feet"
        ],
        [],
        {1: [("Spell Descriptions", 3), ("Fireball", 5)]},
    )

    assert "### Spell Descriptions" in content
    assert "##### Fireball" in content
    assert metadata["matched_visual_headings"] == 2
    assert warnings == ()


def test_pdf_normalization_does_not_treat_uncased_cjk_body_as_all_caps() -> None:
    content, metadata, warnings = build_structured_markdown(
        ["冒险背景 Adventure Background\n这是一行带 D&D 缩写的中文正文\n下一行继续正文。"],
        [],
    )

    assert "##### 这是一行带 D&D 缩写的中文正文" not in content
    assert "这是一行带 D&D 缩写的中文正文下一行继续正文。" in content
    assert metadata["heading_count"] == 0
    assert warnings == ("no structural headings were recovered",)


def test_pdf_normalization_keeps_toc_entries_out_of_heading_hierarchy() -> None:
    content, metadata, _warnings = build_structured_markdown(
        [
            "目录 Contents\n第一章：双城记\n第二章：坠落\n第三章：阿弗纳斯\n"
            "地点一\n地点二\n地点三\n地点四\n地点五\n地点六\n地点七\n地点八",
            "第一章：双城记\nChapter 1\n正文。",
        ],
        [],
    )

    assert metadata["toc_pages"] == [1]
    assert content.count("# 第一章：双城记") == 1
    assert "# 第二章：坠落" not in content


def test_pdf_normalization_recognizes_letter_spaced_english_contents() -> None:
    content, metadata, _warnings = build_structured_markdown(
        [
            "Ta b l e o f C o n t e n t s\n"
            "Episode 1: Arrival...................... 6\n"
            "Episode 2: Pursuit.....................14\n"
            "Episode 3: The Lair....................21\n"
            "Appendix A: Backgrounds...............87\n"
            "Appendix B: Monsters..................88\n"
            "Appendix C: Items.....................94\n"
            "Map: The Coast..........................4\n"
        ],
        [],
        {1: [("Ta b l e o f C o n t e n t s", 4)]},
    )

    assert metadata["toc_pages"] == [1]
    assert "# Episode 1" not in content
    assert "#### Ta b l e" not in content


def test_pdf_normalization_promotes_only_targeted_top_level_bookmark() -> None:
    content, metadata, _warnings = build_structured_markdown(
        [
            "E pisode 1 : G reenest in F l a m e s\nBody text.\n"
            'Chapter 9 ("Lyn Armaal," area 23)\nReference text.'
        ],
        [DocumentBookmark("Episode 1: Greenest in Flames", 1, 0)],
        {1: [('Chapter 9 ("Lyn Armaal," area 23)', 3)]},
    )

    assert "# Episode 1: Greenest in Flames" in content
    assert '### Chapter 9 ("Lyn Armaal," area 23)' in content
    assert not any(
        line.startswith('# Chapter 9 ("Lyn Armaal," area 23)') for line in content.splitlines()
    )
    assert metadata["matched_bookmarks"] == 1


def test_pdf_normalization_uses_shallowest_structural_outline_depth() -> None:
    content, metadata, _warnings = build_structured_markdown(
        ["BOOK TITLE\nCHAPTER 1: FIREBALL\nBody.\nCHAPTER 2: TROLLSKULL ALLEY\nBody."],
        [
            DocumentBookmark("Book Title", 1, 0),
            DocumentBookmark("Ch. 1: Fireball", 1, 1),
            DocumentBookmark("Ch. 2: Trollskull Alley", 1, 1),
        ],
    )

    assert "# Ch. 1: Fireball" in content
    assert "# Ch. 2: Trollskull Alley" in content
    assert metadata["matched_bookmarks"] == 3


def test_pdf_normalization_deduplicates_outline_anchored_running_header() -> None:
    content, _metadata, _warnings = build_structured_markdown(
        [
            "PART 2: PHANDALIN\nBody.\n",
            "PART 2: PHANDALIN\nContinued body.",
        ],
        [DocumentBookmark("Part 2: Phandalin", 1, 0)],
        {1: [("PART 2: PHANDALIN", 2)], 2: [("PART 2: PHANDALIN", 3)]},
    )

    assert sum(line.startswith("# ") for line in content.splitlines()) == 1


def test_pdf_normalization_keeps_page_heading_over_corrupt_appendix_outline() -> None:
    content, _metadata, _warnings = build_structured_markdown(
        ["APPENDIX B: MONSTERS\nBody."],
        [DocumentBookmark("App. 8: Monsters", 1, 0)],
    )

    assert "# APPENDIX B: MONSTERS" in content
    assert "App. 8" not in content


def test_pdf_normalization_recovers_corrupt_structural_heading_from_outline() -> None:
    content, metadata, _warnings = build_structured_markdown(
        ["CHAPTER 8 ( Wl~TER WIZARDRY\nBody."],
        [DocumentBookmark("Ch. 8: Winter Wizardry", 1, 1)],
    )

    assert "# Ch. 8: Winter Wizardry" in content
    assert metadata["matched_bookmarks"] == 1


def test_pdf_normalization_synthesizes_outline_only_chapter_at_target_page() -> None:
    content, metadata, _warnings = build_structured_markdown(
        ["ANSHOON YEARNS TO RULE WATERDEEP\nBody."],
        [DocumentBookmark("Ch. 8: Winter Wizardry", 1, 1)],
    )

    assert content.startswith("<!-- page: 1 -->\n\n# Ch. 8: Winter Wizardry")
    assert "ANSHOON YEARNS TO RULE WATERDEEP" in content
    assert metadata["matched_bookmarks"] == 0
    assert metadata["synthetic_outline_headings"] == 1


def test_pdf_normalization_does_not_anchor_bookmark_to_body_mention() -> None:
    content, metadata, warnings = build_structured_markdown(
        [
            "THE XORLARRIN ALLIANCE\n"
            "By the time the characters begin their journey to Ironslag, "
            "the drow have already infiltrated Gauntlgrym."
        ],
        [DocumentBookmark("Ironslag", 1, 1)],
    )

    assert "### By the time" not in content
    assert metadata["matched_bookmarks"] == 0
    assert warnings == ("text-bearing bookmark match rate is 0/1; expected at least 95%",)


def test_pdf_normalization_rejects_outline_paragraph_as_a_heading() -> None:
    paragraph = (
        "T VJ 7 w such as a city gate and a distant frontier. "
        "Humans are famous for their adaptability across many diverse lands. " * 8
    ).strip()
    assert len(paragraph) > 500

    content, metadata, warnings = build_structured_markdown(
        [f"HUMANS\n{paragraph}\nHUMAN ETHNICITIES\nBody."],
        [DocumentBookmark(paragraph, 1, 0)],
    )

    assert paragraph in content
    assert f"# {paragraph}" not in content
    assert metadata["bookmark_count"] == 1
    assert metadata["matchable_bookmark_count"] == 0
    assert metadata["matched_bookmarks"] == 0
    assert not any("bookmark match rate" in warning for warning in warnings)


def test_pdf_normalization_escapes_accidental_markdown_heading_in_prose() -> None:
    paragraph = (
        "T VJ 7 w such as a city gate and a distant frontier. "
        "Humans are famous for their adaptability across many diverse lands."
    )

    content, metadata, _warnings = build_structured_markdown(
        [f"HUMANS\n#\n{paragraph}\nHUMAN ETHNICITIES\nBody."],
    )

    assert f"\\# {paragraph}" in content
    assert f"\n# {paragraph}\n" not in content
    assert metadata["heading_count"] == 2


def test_pdf_normalization_excludes_image_only_outline_pages_from_match_rate() -> None:
    content, metadata, warnings = build_structured_markdown(
        [
            "CHAPTER 1: OPENING\nBody.",
            "",
            "",
            "",
        ],
        [
            DocumentBookmark("Chapter 1: Opening", 1, 1),
            DocumentBookmark("DM's Map", 2, 1),
            DocumentBookmark("Player's Map", 3, 1),
            DocumentBookmark("Back Cover", 4, 0),
        ],
    )

    assert "# Chapter 1: Opening" in content
    assert metadata["bookmark_count"] == 4
    assert metadata["matchable_bookmark_count"] == 1
    assert metadata["matched_bookmarks"] == 1
    assert not any("bookmark match rate" in warning for warning in warnings)


def test_pdf_normalization_moves_late_outline_chapter_anchor_to_page_start() -> None:
    content, metadata, _warnings = build_structured_markdown(
        [
            "Decorative drop cap paragraph.\nOne.\nTwo.\nThree.\nFour.\nFive.\n"
            "Six.\nSeven.\nEight.\nNine.\nCh . 1: A Friend in Need\nBody."
        ],
        [DocumentBookmark("Ch . 1: A Friend in Need", 1, 1)],
        {1: [("Ch . 1: A Friend in Need", 3)]},
    )

    assert content.startswith("<!-- page: 1 -->\n\n# Ch. 1: A Friend in Need")
    assert content.count("A Friend in Need") == 1
    assert metadata["matched_bookmarks"] == 1
    assert metadata["synthetic_outline_headings"] == 1


def test_pdf_normalization_drops_corrupt_duplicate_of_outline_chapter() -> None:
    content, _metadata, _warnings = build_structured_markdown(
        [
            "CHAPTER 3: THE SAVAGE FRONTIER\nBody.",
            "CHAPTER 3: TuE SAVAGE FRONTIER\nContinued body.",
        ],
        [DocumentBookmark("Chapter 3: The Savage Frontier", 1, 0)],
        {
            1: [("CHAPTER 3: THE SAVAGE FRONTIER", 2)],
            2: [("CHAPTER 3: TuE SAVAGE FRONTIER", 3)],
        },
    )

    assert "# Chapter 3: The Savage Frontier" in content
    assert "TuE SAVAGE" not in content


def test_pdf_form_metadata_distinguishes_populated_values_from_blank_fields() -> None:
    class FormReader:
        @staticmethod
        def get_fields():
            return {
                "Front_Character Name": {"/V": "Smalls"},
                "Front_Level": {"/V": None},
                "Front_Save Int": {"/V": "/Yes"},
                "Unused": {"/V": "/Off"},
            }

    metadata = _pdf_form_metadata(FormReader())

    assert metadata["form_field_count"] == 4
    assert metadata["populated_form_field_count"] == 2
    assert metadata["populated_form_fields"] == {
        "Front_Character Name": "Smalls",
        "Front_Save Int": "/Yes",
    }


def test_pdf_normalization_does_not_promote_chapter_references_in_body() -> None:
    content, metadata, _warnings = build_structured_markdown(
        [
            "Adventure Overview\n正文从这里开始。\n第一章：双城记\n第二章：坠落\n继续说明。",
            "第二章 埃尔托瑞尔已然坠落\nChapter 2: Elturel Has Fallen\n正文。",
        ],
        [],
    )

    assert "# 第一章：双城记" not in content
    assert "# 第二章：坠落" not in content
    assert content.count("# 第二章 埃尔托瑞尔已然坠落") == 1
    assert "# Chapter 2" not in content
    assert metadata["heading_count"] == 1


def test_campaign_profile_events_snapshot_and_memory(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Branches", state={"door": "closed"})
    RuleProfileService(database).set(
        campaign.id,
        edition="2014",
        locale="zh",
        publications=["srd-5.1"],
    )
    character = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Mira",
        sheet={
            "hp": 10,
            "inventory": [{"id": "healing-potion", "equipped": False}],
            "effects": [{"id": "bless", "remaining_turns": 3}],
        },
        notes={"memories": [{"summary": "Trusts the gate guard."}]},
    )
    EventService(database).add(campaign.id, summary="The door is found")
    memory = MemoryService(database).add(
        campaign.id,
        subject="Door",
        content="The cellar door is locked.",
    )
    modules = ModuleService(database)
    modules.ingest(
        campaign_id=campaign.id,
        source_key="split-party.md",
        title="Split Party",
        content="# Chapter\n## Gate\nOutside.\n## Cellar\nBelow.",
    )
    scenes = modules.scene_index(campaign.id)
    modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=scenes[0]["scene_id"],
        scope_id="party",
    )
    modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=scenes[1]["scene_id"],
        scope_id="player:mira",
        state={"private_discoveries": ["whisper"]},
    )

    saves = SnapshotService(database)
    first = saves.create(campaign.id, label="Before opening")
    assert saves.get(campaign.id, first.slot)["recap"]["summary"] == "Campaign baseline"
    payload = saves.get(campaign.id, first.slot)["payload"]
    assert payload["events"][0]["summary"] == "The door is found"
    assert payload["memories"][0]["revision"]["content"].endswith("locked.")
    assert payload["memories"][0]["fact_key"].startswith("memory:")
    assert payload["memories"][0]["revision"]["status"] == "active"
    campaigns.update(campaign.id, state={"door": "open"})
    CharacterService(database).update(character.id, sheet={"hp": 4}, notes={"memories": []})
    MemoryService(database).revise(memory.id, content="The cellar door is open.")
    EventService(database).add(campaign.id, summary="The door is opened")
    modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=scenes[0]["scene_id"],
        scope_id="player:mira",
        state={"private_discoveries": []},
    )
    restored = saves.restore(campaign.id, first.slot)

    assert restored.parent_id == first.id
    assert campaigns.get(campaign.id).state == {"door": "closed"}
    restored_character = CharacterService(database).get(character.id)
    assert restored_character.sheet == {
        "hp": 10,
        "inventory": [{"id": "healing-potion", "equipped": False}],
        "effects": [{"id": "bless", "remaining_turns": 3}],
    }
    assert restored_character.notes == {"memories": [{"summary": "Trusts the gate guard."}]}
    assert MemoryService(database).list(campaign.id)[0].content.endswith("locked.")
    assert [item.summary for item in EventService(database).list(campaign.id)] == [
        "The door is found"
    ]
    assert modules.current_scene(campaign.id)["title"] == "Gate"
    mira_scene = modules.current_scene(campaign.id, scope_id="player:mira")
    assert mira_scene["title"] == "Cellar"
    assert mira_scene["progress"]["state"] == {"private_discoveries": ["whisper"]}
    assert saves.verify(campaign.id, restored.slot)
    assert [item.slot for item in saves.lineage(campaign.id)] == [first.slot, restored.slot]
    recap = saves.regenerate_recap(campaign.id, restored.slot)
    assert recap["source"] == "deterministic"


def test_campaign_memory_upsert_has_stable_identity_and_optimistic_revision(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Stable facts")
    memories = MemoryService(database)
    created = memories.upsert(
        campaign.id,
        fact_key="location:cellar:door-state",
        subject="Cellar door",
        subject_ref="location:cellar",
        predicate="door-state",
        content="The cellar door is locked.",
        importance=4,
        disclosure_scope="party",
    )

    updated = memories.upsert(
        campaign.id,
        fact_key="location:cellar:door-state",
        content="The cellar door is open.",
        expected_revision_id=created.revision_id,
        source_event_ids=["event:door-opened"],
        importance=5,
        disclosure_scope="public",
    )

    assert updated.id == created.id
    assert updated.revision_id != created.revision_id
    assert updated.fact_key == "location:cellar:door-state"
    assert updated.subject == "Cellar door"
    assert updated.subject_ref == "location:cellar"
    assert updated.predicate == "door-state"
    assert updated.source_event_ids == ["event:door-opened"]
    assert updated.importance == 5
    assert updated.disclosure_scope == "public"
    assert len(memories.list(campaign.id)) == 1

    with pytest.raises(ValueError, match="fact_key identity conflict.*subject"):
        memories.upsert(
            campaign.id,
            fact_key="location:cellar:door-state",
            subject="Conflicting identity",
            content="An invalid writer tries to rename the fact.",
            expected_revision_id=updated.revision_id,
        )

    with pytest.raises(ValueError, match="current revision"):
        memories.upsert(
            campaign.id,
            fact_key="location:cellar:door-state",
            content="A stale writer tries to close it.",
            expected_revision_id=created.revision_id,
        )


def test_campaign_memory_upsert_reuses_identity_on_a_sibling_branch(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Branch facts")
    memories = MemoryService(database)
    snapshots = SnapshotService(database)
    branches = BranchService(database)
    base = snapshots.create(campaign.id, label="Before either branch learns the fact")
    main = branches.current(campaign.id)
    alternate = branches.create(
        campaign.id,
        name="alternate-fact",
        from_snapshot_id=base.id,
    )

    main_value = memories.upsert(
        campaign.id,
        fact_key="location:cellar:door-state",
        content="The cellar door is open.",
    )
    snapshots.create(campaign.id, label="Main branch fact")
    snapshots.checkout_branch(campaign.id, alternate.id)
    alternate_value = memories.upsert(
        campaign.id,
        fact_key="location:cellar:door-state",
        content="The cellar door is still locked.",
    )

    assert alternate_value.id == main_value.id
    assert alternate_value.revision_id != main_value.revision_id
    assert memories.list(campaign.id, branch_id=main.id)[0].content.endswith("open.")
    assert memories.list(campaign.id, branch_id=alternate.id)[0].content.endswith("locked.")


def test_campaign_memory_hides_inactive_heads_by_default(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Fact lifecycle")
    memories = MemoryService(database)
    created = memories.upsert(
        campaign.id,
        fact_key="quest:bell:status",
        content="The bell quest is active.",
    )
    retracted = memories.revise(
        created.id,
        content="The bell quest was based on false information.",
        status="retracted",
        expected_revision_id=created.revision_id,
    )

    assert memories.list(campaign.id) == []
    assert memories.search(campaign.id, "bell") == []
    inactive = memories.list(campaign.id, include_inactive=True)
    assert [item.id for item in inactive] == [retracted.id]
    assert [item.revision_id for item in inactive] == [retracted.revision_id]
    assert [item.status for item in inactive] == ["retracted"]


def test_continuity_commit_persists_event_facts_knowledge_and_snapshot_atomically(
    database,
) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Atomic continuity")
    actor = CharacterService(database).create(
        system_id="dnd5e", campaign_id=campaign.id, name="Witness", character_type="pc"
    )

    result = ContinuityCommitService(database).commit(
        campaign.id,
        event={
            "summary": "The witness opens the sealed eastern door.",
            "audience_scope": "actor",
        },
        facts=[
            {
                "fact_key": "location:east-door:state",
                "subject": "Eastern door",
                "subject_ref": "location:east-door",
                "predicate": "state",
                "content": "The eastern door is open.",
                "disclosure_scope": "party",
            }
        ],
        actor_knowledge=[
            {
                "actor_id": actor.id,
                "knowledge_key": "east-door-open",
                "proposition": "I opened the eastern door.",
                "disclosure_scope": "owner",
            }
        ],
        snapshot={"label": "Eastern door opened"},
    )

    assert result["facts"][0]["source_event_ids"] == [result["event"]["id"]]
    assert result["actor_knowledge"][0]["source_event_id"] == result["event"]["id"]
    assert result["snapshot"] is not None
    assert SnapshotService(database).verify(campaign.id, result["snapshot"]["slot"])


def test_continuity_commit_upserts_same_fact_key_on_a_sibling_branch(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Branch continuity")
    snapshots = SnapshotService(database)
    branches = BranchService(database)
    base = snapshots.create(campaign.id, label="Before the branch fact")
    main = branches.current(campaign.id)
    alternate = branches.create(
        campaign.id,
        name="alternate-continuity",
        from_snapshot_id=base.id,
    )
    commits = ContinuityCommitService(database)

    main_result = commits.commit(
        campaign.id,
        event={"summary": "The main branch alerts the guards."},
        facts=[
            {
                "fact_key": "hideout:guards:alerted",
                "content": "The guards are alerted on the main branch.",
            }
        ],
    )
    snapshots.create(campaign.id, label="Main branch continuity")
    snapshots.checkout_branch(campaign.id, alternate.id)
    alternate_result = commits.commit(
        campaign.id,
        event={"summary": "The alternate branch alerts the guards."},
        facts=[
            {
                "fact_key": "hideout:guards:alerted",
                "content": "The guards are alerted independently on the alternate branch.",
            }
        ],
    )

    main_fact = main_result["facts"][0]
    alternate_fact = alternate_result["facts"][0]
    assert alternate_fact["id"] == main_fact["id"]
    assert alternate_fact["revision_id"] != main_fact["revision_id"]
    facts = MemoryService(database)
    assert facts.list(campaign.id, branch_id=main.id)[0].content.endswith("main branch.")
    assert facts.list(campaign.id, branch_id=alternate.id)[0].content.endswith("alternate branch.")


def test_continuity_commit_rolls_back_every_ledger_on_failure(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Rollback continuity")
    service = ContinuityCommitService(database)

    with pytest.raises(ValueError, match="live character"):
        service.commit(
            campaign.id,
            event={"summary": "This event must roll back."},
            facts=[
                {
                    "fact_key": "rollback:test",
                    "content": "This fact must roll back.",
                }
            ],
            actor_knowledge=[
                {
                    "actor_id": "missing-actor",
                    "knowledge_key": "impossible",
                    "proposition": "This must fail.",
                }
            ],
        )

    assert EventService(database).list(campaign.id) == []
    assert MemoryService(database).list(campaign.id, include_inactive=True) == []
    event = EventService(database).add(campaign.id, summary="The first committed event")
    assert event.sequence == 1


def test_revision_undo_and_redo(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="coc7e", name="Arkham", state={"clock": 1})
    campaigns.update(campaign.id, state={"clock": 2})
    revisions = RevisionService(database)
    revisions.record(
        campaign.id,
        operation="campaign.state",
        entity_type="campaign",
        entity_id=campaign.id,
        before={"state": {"clock": 1}},
        after={"state": {"clock": 2}},
    )

    revisions.undo(campaign.id)
    assert campaigns.get(campaign.id).state == {"clock": 1}
    revisions.redo(campaign.id)
    assert campaigns.get(campaign.id).state == {"clock": 2}


def test_campaign_character_is_an_independent_library_instance(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Instances")
    characters = CharacterService(database)
    template = characters.create(
        system_id="dnd5e",
        name="Mira Template",
        character_type="pc",
        sheet={"hp": 10, "inventory": [{"id": "key"}]},
        notes={"profile": {"summary": "A careful explorer."}},
    )
    instance = characters.instantiate(
        template.id,
        campaign_id=campaign.id,
        name="Mira",
        player_name="Ada",
        sheet={"hp": 9, "inventory": [{"id": "key"}], "edition": "2024"},
    )

    assert instance.id != template.id
    assert instance.template_id == template.id
    assert instance.campaign_id == campaign.id
    assert instance.sheet["edition"] == "2024"
    assert [item.id for item in characters.list_library(system_id="dnd5e")] == [template.id]

    characters.update(instance.id, sheet={"hp": 4, "inventory": []})
    assert characters.get(template.id).sheet == {
        "hp": 10,
        "inventory": [{"id": "key"}],
    }

    snapshot = SnapshotService(database).create(campaign.id, label="Template instance")
    characters.update(instance.id, sheet={"hp": 1, "inventory": []})
    characters.update(template.id, notes={"profile": {"summary": "Updated library copy."}})
    SnapshotService(database).restore(campaign.id, snapshot.slot)

    assert characters.get(instance.id).sheet["hp"] == 4
    assert characters.get(template.id).notes["profile"]["summary"] == "Updated library copy."


def test_character_build_creates_template_and_instance_atomically(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Build")
    template, instance = CharacterService(database).create_with_instance(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Mira",
        character_type="pc",
        player_name="Ada",
        sheet={"hp": 10},
        notes={"profile": {"summary": "A newly built hero."}},
    )

    assert template.campaign_id is None
    assert template.player_name is None
    assert instance.campaign_id == campaign.id
    assert instance.player_name == "Ada"
    assert instance.template_id == template.id
    assert instance.sheet == template.sheet


def test_character_build_replays_template_and_instance_atomically(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Build replay")
    characters = CharacterService(database)
    arguments = {
        "system_id": "dnd5e",
        "campaign_id": campaign.id,
        "name": "Mira",
        "character_type": "pc",
        "player_name": "Ada",
        "sheet": {"hp": 10},
        "notes": {"profile": {"summary": "A newly built hero."}},
        "principal_id": "dm:ada",
        "idempotency_key": "build-mira",
    }

    first = characters.create_with_instance(**arguments)
    replay = characters.create_with_instance(**arguments)

    assert replay[0].id == first[0].id
    assert replay[1].id == first[1].id
    assert [item.id for item in characters.list_library(system_id="dnd5e")] == [first[0].id]
    assert [item.id for item in characters.list(system_id="dnd5e", campaign_id=campaign.id)] == [
        first[1].id
    ]


def test_snapshot_restore_preserves_its_undo_cursor_and_retires_future_revisions(
    database,
) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Undo branch", state={"clock": 0})
    revisions = RevisionService(database)
    snapshots = SnapshotService(database)

    campaigns.update(campaign.id, state={"clock": 1})
    revisions.record(
        campaign.id,
        operation="campaign.state",
        entity_type="campaign",
        entity_id=campaign.id,
        before={"state": {"clock": 0}},
        after={"state": {"clock": 1}},
    )
    saved = snapshots.create(campaign.id, label="Clock one")

    campaigns.update(campaign.id, state={"clock": 2})
    revisions.record(
        campaign.id,
        operation="campaign.state",
        entity_type="campaign",
        entity_id=campaign.id,
        before={"state": {"clock": 1}},
        after={"state": {"clock": 2}},
    )
    snapshots.restore(campaign.id, saved.slot)

    assert campaigns.get(campaign.id).state == {"clock": 1}
    with pytest.raises(LookupError, match="nothing to redo"):
        revisions.redo(campaign.id)
    revisions.undo(campaign.id)
    assert campaigns.get(campaign.id).state == {"clock": 0}


def test_snapshot_restore_rolls_back_every_step_when_materialization_fails(
    database, monkeypatch
) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Atomic restore")
    snapshots = SnapshotService(database)
    target = snapshots.create(campaign.id, label="target")
    branches = BranchService(database)
    original_branch = branches.current(campaign.id)
    original_slots = [item.slot for item in snapshots.list(campaign.id)]

    def fail_apply(*_args, **_kwargs) -> None:
        raise RuntimeError("materialization failed")

    monkeypatch.setattr(snapshots, "_apply", fail_apply)

    with pytest.raises(RuntimeError, match="materialization failed"):
        snapshots.restore(campaign.id, target.slot)

    assert [item.slot for item in snapshots.list(campaign.id)] == original_slots
    assert [item.id for item in branches.list(campaign.id)] == [original_branch.id]
    assert branches.current(campaign.id).id == original_branch.id


def test_snapshot_rule_profile_conversion_forks_without_mutating_source(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Core conversion", state={"step": 1})
    profiles = RuleProfileService(database)
    profiles.set(
        campaign.id,
        edition="2014",
        locale="zh-CN",
        publications=["srd-5.1"],
        options={"_core_rule_pack_lock": {"version": "old"}, "house_rule": True},
    )
    snapshots = SnapshotService(database)
    source = snapshots.create(campaign.id, label="Old core")
    source_document = snapshots.get(campaign.id, source.slot)
    campaigns.update(campaign.id, state={"step": 2})

    converted_profile = {
        **source_document["payload"]["rule_profile"],
        "options": {
            "_core_rule_pack_lock": {"version": "new"},
            "house_rule": True,
        },
    }
    converted = snapshots.restore_with_rule_profile_conversion(
        campaign.id,
        source.slot,
        rule_profile=converted_profile,
        branch_name="converted-core",
        label="Converted old core snapshot",
    )

    assert converted.parent_id == source.id
    assert campaigns.get(campaign.id).state == {"step": 1}
    assert BranchService(database).current(campaign.id).name == "converted-core"
    assert profiles.get(campaign.id).options == converted_profile["options"]
    assert "edition" not in campaigns.get(campaign.id).settings
    assert "locale" not in campaigns.get(campaign.id).settings
    source_after = snapshots.get(campaign.id, source.slot)
    assert source_after["payload"] == source_document["payload"]
    assert source_after["checksum"] == source_document["checksum"]
    converted_document = snapshots.get(campaign.id, converted.slot)
    assert converted_document["payload"]["rule_profile"] == converted_profile
    assert converted_document["valid"] is True


def test_rule_profile_change_and_exact_receipt_commit_together(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Atomic profile")
    payload = {"edition": "2014", "locale": "en"}
    profiles = RuleProfileService(database)

    profiles.set(
        campaign.id,
        edition="2014",
        expected_campaign_revision=campaign.revision,
        idempotency_key="profile",
        idempotency_write=IdempotencyWrite(
            scope=f"rule-profile:{campaign.id}",
            payload=payload,
            response=lambda result: {
                "edition": result["profile"].edition,
                "campaign_revision": result["campaign_revision"],
            },
        ),
    )

    replay = IdempotencyService(database).lookup(
        f"rule-profile:{campaign.id}",
        "profile",
        payload,
    )
    assert replay is not None
    assert replay.response == {
        "edition": "2014",
        "campaign_revision": campaign.revision + 1,
    }


def test_branch_scoped_facts_events_and_actor_knowledge_do_not_leak(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Knowledge branches")
    actor = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Guard",
        character_type="npc",
        sheet={},
        notes={},
    )
    events = EventService(database)
    memories = MemoryService(database)
    knowledge = ActorKnowledgeService(database)
    snapshots = SnapshotService(database)

    witnessed = events.add(campaign.id, summary="The guard sees the cellar key")
    fact = memories.add(campaign.id, subject="Cellar key", content="The key is in the cellar.")
    belief = knowledge.add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="cellar-key-location",
        proposition="The key is in the cellar.",
        source_event_id=witnessed.id,
    )
    base = snapshots.create(campaign.id, label="Key seen")
    main = BranchService(database).current(campaign.id)

    memories.revise(fact.id, content="The key is now in the guard room.")
    knowledge.revise(
        belief.id,
        proposition="The key was moved to the guard room.",
        epistemic_status="belief",
    )
    events.add(campaign.id, summary="The key is moved")
    snapshots.create(campaign.id, label="Key moved")

    alternate = BranchService(database).create(
        campaign.id,
        name="key-stays-put",
        from_snapshot_id=base.id,
        checkout=True,
    )
    snapshots.checkout_branch(campaign.id, alternate.id)

    assert memories.list(campaign.id)[0].content == "The key is in the cellar."
    assert knowledge.list(campaign.id, actor_id=actor.id)[0].proposition.endswith("cellar.")
    assert [item.summary for item in events.list(campaign.id)] == ["The guard sees the cellar key"]

    assert memories.list(campaign.id, branch_id=main.id)[0].content.endswith("guard room.")
    assert knowledge.list(campaign.id, actor_id=actor.id, branch_id=main.id)[
        0
    ].proposition.endswith("guard room.")


def test_event_and_all_witness_knowledge_commit_or_rollback_together(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Atomic witnesses")
    characters = CharacterService(database)
    first = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="First", character_type="pc"
    )
    second = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Second", character_type="pc"
    )
    third = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Third", character_type="pc"
    )
    events = EventService(database)
    knowledge = ActorKnowledgeService(database)

    event, knowledge_ids = events.add_with_actor_knowledge(
        campaign.id,
        summary="First and second see the sigil.",
        actor_ids=[first.id, second.id],
        knowledge_key="sigil",
        proposition="The sigil is blue.",
        audience_scope="party",
    )
    assert len(knowledge_ids) == 2
    assert knowledge.list(campaign.id, actor_id=first.id)[0].source_event_id == event.id
    assert knowledge.list(campaign.id, actor_id=second.id)[0].source_event_id == event.id

    with pytest.raises(ValueError, match="knowledge key already exists"):
        events.add_with_actor_knowledge(
            campaign.id,
            summary="This write must fully roll back.",
            actor_ids=[third.id, first.id],
            knowledge_key="sigil",
            proposition="A conflicting observation.",
        )

    assert [item.summary for item in events.list(campaign.id)] == [
        "First and second see the sigil."
    ]
    assert knowledge.list(campaign.id, actor_id=third.id) == []


def test_actor_scoped_events_follow_visible_actor_knowledge(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Separate witnesses")
    characters = CharacterService(database)
    witness = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Witness", character_type="pc"
    )
    unaware = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Unaware", character_type="pc"
    )
    event = EventService(database).add(
        campaign.id,
        event_type="revelation",
        summary="The witness sees the masked visitor leave.",
        audience_scope="actor",
    )
    ActorKnowledgeService(database).add(
        campaign.id,
        actor_id=witness.id,
        knowledge_key="masked-visitor-departed",
        proposition="The masked visitor left by the east door.",
        source_event_id=event.id,
        disclosure_scope="owner",
    )
    continuity = ContinuityService(database)

    seen = continuity.context(
        campaign.id,
        actor_id=witness.id,
        audience="player",
        query="masked visitor",
    )
    hidden = continuity.context(
        campaign.id,
        actor_id=unaware.id,
        audience="player",
        query="masked visitor",
    )
    events = EventService(database)
    witness_log = events.list_for_audience(
        campaign.id,
        audience="player",
        actor_id=witness.id,
    )
    unaware_log = events.list_for_audience(
        campaign.id,
        audience="player",
        actor_id=unaware.id,
    )
    unscoped_player_log = events.list_for_audience(
        campaign.id,
        audience="player",
    )

    assert [item["id"] for item in seen["events"]] == [event.id]
    assert [item["knowledge_key"] for item in seen["actor_knowledge"]] == [
        "masked-visitor-departed"
    ]
    assert hidden["events"] == []
    assert hidden["actor_knowledge"] == []
    assert [item.id for item in witness_log] == [event.id]
    assert unaware_log == []
    assert unscoped_player_log == []


def test_event_retrieval_text_drives_shared_budget_selection(database) -> None:
    campaign = CampaignService(database).create(system_id="neutral", name="Event retrieval")
    events = EventService(database)
    relevant = events.add(
        campaign.id,
        event_type="npc_conversation",
        summary="A conversation occurred.",
        retrieval_text="The obsidian key is hidden beneath the bridge.",
    )
    stored = events.list(campaign.id)[0]
    assert stored.retrieval_text == "The obsidian key is hidden beneath the bridge."

    padding = "x" * 700
    selected, _ = ContinuityService._apply_budget(
        query="obsidian key",
        facts=[],
        knowledge=[],
        events=[
            {
                "id": relevant.id,
                "sequence": 1,
                "event_type": "npc_conversation",
                "summary": "A conversation occurred.",
                "retrieval_text": "The obsidian key is hidden beneath the bridge.",
                "padding": padding,
            },
            {
                "id": "irrelevant",
                "sequence": 2,
                "event_type": "npc_conversation",
                "summary": "Another conversation occurred.",
                "retrieval_text": "Nothing related was discussed.",
                "padding": padding,
            },
        ],
        budget_chars=1_000,
    )
    assert [item["id"] for item in selected["events"]] == [relevant.id]


def test_actor_scoped_events_follow_explicit_participants_without_fake_knowledge(
    database,
) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Private conversation")
    characters = CharacterService(database)
    speaker = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Speaker", character_type="npc"
    )
    listener = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Listener", character_type="pc"
    )
    outsider = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Outsider", character_type="pc"
    )
    events = EventService(database)
    event = events.add(
        campaign.id,
        event_type="npc_dialogue_turn",
        summary="The speaker whispers an opinion that establishes no durable fact.",
        audience_scope="actor",
        participants=[
            {"actor_id": speaker.id, "role": "speaker"},
            {"actor_id": listener.id, "role": "listener"},
        ],
    )

    assert [item.id for item in events.list_for_actor(campaign.id, actor_id=speaker.id)] == [
        event.id
    ]
    assert [
        item.id
        for item in events.list_for_audience(
            campaign.id,
            audience="player",
            actor_id=listener.id,
        )
    ] == [event.id]
    assert (
        events.list_for_audience(
            campaign.id,
            audience="player",
            actor_id=outsider.id,
        )
        == []
    )
    assert ActorKnowledgeService(database).list(campaign.id, actor_id=listener.id) == []
    assert event.participants == (
        {"actor_id": listener.id, "role": "listener"},
        {"actor_id": speaker.id, "role": "speaker"},
    )


def test_continuity_commit_snapshots_event_participants_and_detects_index_tampering(
    database,
) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Dialogue snapshot")
    characters = CharacterService(database)
    speaker = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Speaker", character_type="npc"
    )
    listener = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Listener", character_type="pc"
    )
    result = ContinuityCommitService(database).commit(
        campaign.id,
        event={
            "event_type": "npc_dialogue_turn",
            "summary": "A private exchange occurs.",
            "audience_scope": "actor",
            "participants": [
                {"actor_id": speaker.id, "role": "speaker"},
                {"actor_id": listener.id, "role": "listener"},
            ],
        },
        snapshot={"label": "After private exchange"},
    )

    slot = result["snapshot"]["slot"]
    assert SnapshotService(database).verify(campaign.id, slot)
    with database.transaction() as session:
        session.execute(
            delete(CampaignEventParticipant).where(
                CampaignEventParticipant.event_id == result["event"]["id"],
                CampaignEventParticipant.actor_id == listener.id,
            )
        )
    assert not SnapshotService(database).verify(campaign.id, slot)


def test_exact_memory_subject_projection_avoids_lexical_cross_actor_leaks(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Actor state")
    memories = MemoryService(database)
    zaltember = memories.upsert(
        campaign.id,
        fact_key="actor.relationship:zaltember:party",
        subject_ref="actor:zaltember",
        predicate="relationship_to",
        kind="actor_state",
        content="Zaltember distrusts the party.",
    )
    memories.upsert(
        campaign.id,
        fact_key="actor.relationship:duke-zalto:party",
        subject_ref="actor:duke-zalto",
        predicate="relationship_to",
        kind="actor_state",
        content="Duke Zalto hates the party.",
    )

    projected = memories.list_for_subject_refs(
        campaign.id,
        subject_refs={"actor:zaltember"},
        predicates={"relationship_to", "goal"},
        kinds={"actor_state"},
    )

    assert [item.id for item in projected] == [zaltember.id]


def test_actor_event_authorization_is_not_limited_by_knowledge_top_n(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Recall window")
    actor = CharacterService(database).create(
        system_id="dnd5e", campaign_id=campaign.id, name="Witness", character_type="pc"
    )
    knowledge = ActorKnowledgeService(database)
    knowledge.add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="decoy-query-match",
        proposition="The decoy query is memorable.",
        disclosure_scope="owner",
    )
    event = EventService(database).add(
        campaign.id,
        summary="A masked courier leaves through the east door.",
        audience_scope="actor",
    )
    knowledge.add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="courier-departure",
        proposition="The masked courier left through the east door.",
        source_event_id=event.id,
        disclosure_scope="owner",
    )

    context = ContinuityService(database).context(
        campaign.id,
        actor_id=actor.id,
        audience="player",
        query="decoy query",
        limit=1,
    )

    assert [item["knowledge_key"] for item in context["actor_knowledge"]] == ["decoy-query-match"]
    assert [item["id"] for item in context["events"]] == [event.id]


def test_player_continuity_redacts_restricted_scene_content_and_progress_state(database) -> None:
    campaign = CampaignService(database).create(system_id="neutral", name="Private scene")
    modules = ModuleService(database)
    modules.ingest(
        campaign_id=campaign.id,
        source_key="private-scene.md",
        title="Private Scene",
        content="# Chapter\n## Hidden Bargain\nThe captive can be exchanged for the relic.",
    )
    scene = modules.scene_index(campaign.id)[0]
    progress = modules.set_scene_progress(
        campaign_id=campaign.id,
        scene_id=scene["scene_id"],
        state={"gm_secret": "the relic is hidden below the throne"},
    )

    context = ContinuityService(database).context(
        campaign.id,
        audience="player",
    )

    projected = context["scoped_scene"]
    assert projected["redacted"] is True
    assert projected["content"] == "[Restricted scene content hidden]"
    assert projected["progress"] == {
        "status": "current",
        "percent": 0,
        "state_version": progress["state_version"],
    }
    assert "gm_secret" not in str(projected)


def test_continuity_context_applies_one_shared_budget_with_metrics(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Budgeted context")
    memories = MemoryService(database)
    for index in range(5):
        memories.add(
            campaign.id,
            fact_key=f"fact:budget:{index}",
            content=f"Matched clue {index} " + ("x" * 360),
            importance=5 - min(index, 4),
            disclosure_scope="party",
        )

    context = ContinuityService(database).context(
        campaign.id,
        query="matched clue",
        audience="player",
        budget_chars=1_000,
    )

    assert context["retrieval"]["strategy"] == "lexical_structured_shared_budget_v2"
    assert context["retrieval"]["budget_chars"] == 1_000
    assert context["retrieval"]["candidate_count"] == 5
    assert context["retrieval"]["returned_count"] < 5
    assert context["retrieval"]["truncated"] is True


def test_context_anchor_pins_exact_dm_module_evidence_without_encoding_behavior(
    database,
) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Pinned module context",
    )
    modules = ModuleService(database)
    modules.ingest(
        campaign_id=campaign.id,
        source_key="ironslag.md",
        title="Ironslag",
        content=(
            "# Forge of the Fire Giants\n"
            "## Foundry Upper Level\n"
            "Zaltember is a bully and coward. If wounded, he flees to area 31. "
            "If captured, his parents first try to secure his release as a show "
            "of good faith before yielding the conch.\n"
        ),
    )
    expanded = modules.expand(
        modules.search(
            campaign_id=campaign.id,
            query="Zaltember wounded captured conch",
        )[0].id
    )
    source_ref = expanded["source_ref"]
    metadata = {
        "schema_version": 1,
        "purpose": "Zaltember behavior and conch negotiation source",
        "related_refs": [
            "scene:foundry-upper-level",
            "quest:obtain-fire-giant-conch",
        ],
        "source_bindings": [
            {
                "source_ref": source_ref,
                "source_excerpt": (
                    "Zaltember is a bully and coward. If wounded, he flees to area 31."
                ),
            }
        ],
    }
    anchor = MemoryService(database).add(
        campaign.id,
        fact_key="context:actor:zaltember:ironslag",
        kind="context_anchor",
        subject="Zaltember module context",
        subject_ref="actor:zaltember",
        content="Exact module context for Zaltember.",
        metadata=metadata,
        disclosure_scope="dm",
    )
    MemoryService(database).add(
        campaign.id,
        fact_key="context:item:fire-giant-conch:ironslag",
        kind="context_anchor",
        subject="Fire giant conch module context",
        subject_ref="item:fire-giant-conch",
        content="The same exact source may support more than one entity link.",
        metadata={
            **metadata,
            "purpose": "Fire giant conch negotiation source",
        },
        disclosure_scope="dm",
    )
    SnapshotService(database).create(campaign.id, label="Context anchored")
    alternate = BranchService(database).create(
        campaign.id,
        name="context-restore",
        checkout=False,
    )

    context = ContinuityService(database).context(
        campaign.id,
        query="unrelated query that cannot retrieve the source lexically",
        related_refs=["actor:zaltember"],
        budget_chars=1_000,
    )
    restored = ContinuityService(database).context(
        campaign.id,
        branch_id=alternate.id,
        related_refs=["quest:obtain-fire-giant-conch"],
        budget_chars=1_000,
    )
    player = ContinuityService(database).context(
        campaign.id,
        audience="player",
        related_refs=["actor:zaltember"],
        budget_chars=1_000,
    )

    assert anchor.metadata["related_refs"] == [
        "actor:zaltember",
        "scene:foundry-upper-level",
        "quest:obtain-fire-giant-conch",
    ]
    assert context["facts"] == []
    assert context["module_evidence"][0]["pinned"] is True
    assert context["module_evidence"][0]["context_role"] == ("non_executable_module_evidence")
    assert context["module_evidence"][0]["anchor_fact_keys"] == ["context:actor:zaltember:ironslag"]
    assert "purposes" not in context["module_evidence"][0]
    assert context["module_evidence"][0]["source_ref"] == source_ref
    assert context["module_evidence"][0]["source_excerpt"].endswith("he flees to area 31.")
    assert context["retrieval"]["strategy"] == ("lexical_structured_pinned_module_evidence_v3")
    assert context["retrieval"]["pinned_module_evidence_count"] == 1
    assert (
        restored["module_evidence"][0]["source_ref"]
        == (context["module_evidence"][0]["source_ref"])
    )
    assert (
        restored["module_evidence"][0]["source_excerpt"]
        == (context["module_evidence"][0]["source_excerpt"])
    )
    assert restored["module_evidence"][0]["matched_refs"] == ["quest:obtain-fire-giant-conch"]
    assert restored["module_evidence"][0]["anchor_fact_keys"] == [
        "context:actor:zaltember:ironslag",
        "context:item:fire-giant-conch:ironslag",
    ]
    assert restored["retrieval"]["pinned_module_evidence_count"] == 1
    assert player["module_evidence"] == []


def test_context_anchor_rejects_conditions_player_visibility_and_paraphrased_source(
    database,
) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Strict context anchors",
    )
    modules = ModuleService(database)
    modules.ingest(
        campaign_id=campaign.id,
        source_key="strict.md",
        title="Strict",
        content="# Chapter\n## Scene\nThe guard retreats when wounded.\n",
    )
    expanded = modules.expand(modules.search(campaign_id=campaign.id, query="guard retreats")[0].id)
    metadata = {
        "schema_version": 1,
        "purpose": "Guard behavior source",
        "related_refs": [],
        "source_bindings": [
            {
                "source_ref": expanded["source_ref"],
                "source_excerpt": "The guard invents a different response.",
            }
        ],
    }
    with pytest.raises(ValueError, match="predicate"):
        MemoryService(database).add(
            campaign.id,
            fact_key="context:guard:predicate",
            kind="context_anchor",
            subject_ref="actor:guard",
            predicate="hp.value < hp.max",
            content="Must fail.",
            metadata=metadata,
            disclosure_scope="dm",
        )
    with pytest.raises(ValueError, match="DM-only"):
        MemoryService(database).add(
            campaign.id,
            fact_key="context:guard:party",
            kind="context_anchor",
            subject_ref="actor:guard",
            content="Must fail.",
            metadata=metadata,
            disclosure_scope="party",
        )
    anchor = MemoryService(database).add(
        campaign.id,
        fact_key="context:guard:bad-excerpt",
        kind="context_anchor",
        subject_ref="actor:guard",
        content="Shape is valid until its source is resolved.",
        metadata=metadata,
        disclosure_scope="dm",
    )
    with pytest.raises(ValueError, match="source_excerpt is absent"):
        ContinuityService(database).context(
            campaign.id,
            related_refs=[anchor.subject_ref],
        )


def test_continuity_diagnostics_reports_ledger_and_snapshot_health(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Diagnostics")
    event = EventService(database).add(
        campaign.id,
        summary="The gate opens.",
        audience_scope="party",
    )
    memory = MemoryService(database).add(
        campaign.id,
        fact_key="location:gate:state",
        content="The gate is open.",
        source_event_ids=[event.id, "event:missing"],
        disclosure_scope="party",
    )
    SnapshotService(database).create(campaign.id, label="Gate opened")
    MemoryService(database).revise(
        memory.id,
        content="The gate is no longer relevant.",
        expected_revision_id=memory.revision_id,
        status="superseded",
    )
    EventService(database).add(
        campaign.id,
        summary="A later event is not snapshotted.",
        audience_scope="dm",
    )

    diagnostics = ContinuityService(database).diagnostics(campaign.id)

    assert diagnostics["facts"] == {
        "total": 1,
        "active": 0,
        "inactive": 1,
        "orphan_source_event_refs": 1,
    }
    assert diagnostics["events"]["unsnapshotted"] == 1
    assert diagnostics["events"]["latest_sequence"] == 2
    assert diagnostics["snapshots"]["total_on_branch"] == 1
    assert diagnostics["snapshots"]["latest_payload_chars"] > 0


def test_event_and_actor_knowledge_reject_unknown_visibility_scopes(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Visibility enums")
    actor = CharacterService(database).create(
        system_id="dnd5e", campaign_id=campaign.id, name="Witness", character_type="pc"
    )
    with pytest.raises(ValueError, match="event audience scope"):
        EventService(database).add(
            campaign.id,
            summary="Invalid audience",
            audience_scope="somebody",
        )
    with pytest.raises(ValueError, match="actor-knowledge disclosure scope"):
        ActorKnowledgeService(database).add(
            campaign.id,
            actor_id=actor.id,
            knowledge_key="invalid-scope",
            proposition="This must be rejected.",
            disclosure_scope="somebody",
        )


def test_snapshot_recap_only_contains_party_safe_deltas(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Safe recap")
    characters = CharacterService(database)
    hero = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Hero", character_type="pc"
    )
    hidden_npc = characters.create(
        system_id="dnd5e", campaign_id=campaign.id, name="Hidden Spy", character_type="npc"
    )
    snapshots = SnapshotService(database)
    snapshots.create(campaign.id, label="Before changes")

    characters.update(hidden_npc.id, summary="The hidden spy changed plans.")
    characters.update(hero.id, summary="The hero was wounded.")
    EventService(database).add(
        campaign.id,
        summary="The party reached the bridge.",
        audience_scope="party",
    )
    EventService(database).add(
        campaign.id,
        summary="The spy poisoned the well.",
        audience_scope="dm",
    )
    MemoryService(database).add(
        campaign.id,
        content="The spy poisoned the well.",
        metadata={"disclosure_scope": "dm"},
    )
    saved = snapshots.create(campaign.id, label="After changes")
    recap = snapshots.get(campaign.id, saved.slot)["recap"]

    assert recap["characters"] == ["Hero"]
    assert recap["events"] == ["The party reached the bridge."]
    assert recap["memory_candidates"] == []
    assert "Hidden Spy" not in str(recap)
    assert "poisoned" not in str(recap)


def test_snapshot_presentation_recap_is_subordinate_and_evidence_bound(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Recap evidence")
    snapshots = SnapshotService(database)
    snapshots.create(campaign.id, label="Baseline")
    visible = EventService(database).add(
        campaign.id,
        summary="The party reaches the bridge.",
        audience_scope="party",
    )
    hidden = EventService(database).add(
        campaign.id,
        summary="The hidden spy leaves.",
        audience_scope="dm",
    )

    saved = snapshots.create(
        campaign.id,
        label="Bridge",
        recap={
            "summary": "We reached the bridge.",
            "evidence_event_ids": [visible.id],
        },
    )
    recap = snapshots.get(campaign.id, saved.slot)["recap"]

    assert recap["source"] == "deterministic"
    assert recap["events"] == ["The party reaches the bridge."]
    assert recap["presentation"]["summary"] == "We reached the bridge."
    assert recap["provenance"]["evidence_event_ids"] == [visible.id]
    with pytest.raises(ValueError, match="outside the player-safe delta"):
        snapshots.create(
            campaign.id,
            label="Unsafe presentation",
            recap={"summary": "Leak", "evidence_event_ids": [hidden.id]},
        )


def test_same_actor_knowledge_key_can_diverge_independently_on_sibling_branches(
    database,
) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Sibling beliefs")
    actor = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Witness",
        character_type="npc",
    )
    snapshots = SnapshotService(database)
    branches = BranchService(database)
    knowledge = ActorKnowledgeService(database)
    base = snapshots.create(campaign.id, label="Before the clue")
    main = branches.current(campaign.id)
    alternate = branches.create(
        campaign.id,
        name="alternate-clue",
        from_snapshot_id=base.id,
    )

    main_value = knowledge.add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="masked-visitor",
        subject_ref="npc:visitor",
        proposition="The visitor wore a red mask.",
    )
    snapshots.create(campaign.id, label="Main sees red")
    snapshots.checkout_branch(campaign.id, alternate.id)
    alternate_value = knowledge.add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="masked-visitor",
        subject_ref="npc:visitor",
        proposition="The visitor wore a blue mask.",
    )

    assert alternate_value.id == main_value.id
    assert alternate_value.revision_id != main_value.revision_id
    assert knowledge.list(campaign.id, actor_id=actor.id, branch_id=main.id)[
        0
    ].proposition.endswith("red mask.")
    assert knowledge.list(campaign.id, actor_id=actor.id, branch_id=alternate.id)[
        0
    ].proposition.endswith("blue mask.")


def test_actor_knowledge_cannot_cite_an_event_from_another_branch(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Event causality")
    actor = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Witness",
        character_type="npc",
    )
    snapshots = SnapshotService(database)
    base = snapshots.create(campaign.id, label="Before split")
    event = EventService(database).add(campaign.id, summary="Only the main branch sees this")
    fact = MemoryService(database).add(
        campaign.id,
        subject="Main-only fact",
        content="Only the main branch recorded this.",
    )
    snapshots.create(campaign.id, label="Main-only event")
    alternate = BranchService(database).create(
        campaign.id,
        name="did-not-see-event",
        from_snapshot_id=base.id,
    )
    snapshots.checkout_branch(campaign.id, alternate.id)

    with pytest.raises(LookupError, match="not visible on branch"):
        MemoryService(database).revise(
            fact.id,
            content="This must not import the fact into the sibling branch.",
        )
    with pytest.raises(LookupError, match="not visible on branch"):
        ActorKnowledgeService(database).add(
            campaign.id,
            actor_id=actor.id,
            knowledge_key="impossible-witness",
            proposition="I saw the main-branch event.",
            source_event_id=event.id,
        )


def test_snapshot_is_full_and_validates_actor_knowledge_bindings(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Full save")
    actor = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Archivist",
        character_type="npc",
        sheet={"hp": 7},
    )
    ActorKnowledgeService(database).add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="sealed-door",
        proposition="The eastern door is sealed.",
    )
    snapshots = SnapshotService(database)
    saved = snapshots.create(campaign.id, label="Complete state")
    document = snapshots.get(campaign.id, saved.slot)

    assert "storage_mode" not in document
    assert document["payload"]["campaign"]["name"] == "Full save"
    assert document["payload"]["characters"][0]["sheet"] == {"hp": 7}
    assert document["payload"]["actor_knowledge"][0]["knowledge_key"] == "sealed-door"
    assert document["valid"] is True

    with database.transaction() as session:
        revision_id = session.scalar(
            select(SnapshotActorKnowledgeBinding.revision_id).where(
                SnapshotActorKnowledgeBinding.snapshot_id == saved.id
            )
        )
        session.get(ActorKnowledgeRevision, revision_id).proposition = "Tampered ledger value."
    assert snapshots.verify(campaign.id, saved.slot) is False
    with pytest.raises(SnapshotIntegrityError, match="wrong revision"):
        snapshots.restore(campaign.id, saved.slot)
    with database.transaction() as session:
        session.get(ActorKnowledgeRevision, revision_id).proposition = "The eastern door is sealed."
    assert snapshots.verify(campaign.id, saved.slot) is True

    with database.transaction() as session:
        session.execute(
            delete(SnapshotActorKnowledgeBinding).where(
                SnapshotActorKnowledgeBinding.snapshot_id == saved.id
            )
        )

    assert snapshots.verify(campaign.id, saved.slot) is False
    with pytest.raises(SnapshotIntegrityError, match="actor-knowledge bindings"):
        snapshots.restore(campaign.id, saved.slot)


def test_snapshot_storage_is_self_contained_compressed_and_parent_bound(database) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Compressed save",
        state={"repeated": "x" * 20_000},
    )
    snapshots = SnapshotService(database)
    base = snapshots.create(campaign.id, label="Compressed base")
    child = snapshots.create(campaign.id, label="Compressed child")

    with database.transaction() as session:
        row = session.get(CampaignSnapshot, child.id)
        assert row is not None
        assert row.payload_codec == "zlib-1"
        assert len(row.compressed_payload) < row.uncompressed_size
        assert len(zlib.decompress(row.compressed_payload)) == row.uncompressed_size
        assert row.schema_version == 8

    document = snapshots.get(campaign.id, child.slot)
    assert "storage_mode" not in document
    assert document["payload"]["campaign"]["state"] == {"repeated": "x" * 20_000}
    assert document["valid"] is True

    with database.transaction() as session:
        row = session.get(CampaignSnapshot, child.id)
        assert row is not None
        row.parent_id = None

    assert snapshots.verify(campaign.id, child.slot) is False
    with pytest.raises(SnapshotIntegrityError, match="record failed checksum"):
        snapshots.restore(campaign.id, child.slot)
    assert snapshots.verify(campaign.id, base.slot) is True


def test_snapshot_verify_rejects_unavailable_module_without_addons(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Module save")
    other = CampaignService(database).create(system_id="dnd5e", name="Other campaign")
    imported = ModuleService(database).ingest(
        campaign_id=campaign.id,
        source_key="module.md",
        title="Module",
        content="# Chapter\n## Scene\nContent.",
    )
    snapshots = SnapshotService(database)
    saved = snapshots.create(campaign.id, label="Module active")
    assert snapshots.get(campaign.id, saved.slot)["payload"]["addon_lock"] == []

    with database.transaction() as session:
        session.get(ModuleSource, imported.module_id).campaign_id = other.id

    assert snapshots.verify(campaign.id, saved.slot) is False
    with pytest.raises(SnapshotIntegrityError, match="unavailable revision"):
        snapshots.restore(campaign.id, saved.slot)


def test_snapshot_integrity_query_count_is_bounded_for_large_event_ledgers(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Large ledger")
    branch_id = BranchService(database).current(campaign.id).id
    with database.transaction() as session:
        session.add_all(
            [
                CampaignEvent(
                    id=f"event-{index:04d}",
                    campaign_id=campaign.id,
                    branch_id=branch_id,
                    sequence=index + 1,
                    event_type="narrative",
                    summary=f"Ledger event {index}",
                    payload={"index": index},
                    audience_scope="dm",
                )
                for index in range(1000)
            ]
        )
        session.add_all(
            [
                CampaignEventParticipant(
                    event_id=f"event-{index:04d}",
                    actor_id="ledger-observer",
                    role="observer",
                )
                for index in range(1000)
            ]
        )
    snapshots = SnapshotService(database)
    saved = snapshots.create(campaign.id, label="Thousand events")
    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record_statement)
    try:
        assert snapshots.verify(campaign.id, saved.slot)
    finally:
        event.remove(database.engine, "before_cursor_execute", record_statement)

    select_statements = [
        statement for statement in statements if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(select_statements) <= 25
    assert sum("campaign_event_participants" in item.casefold() for item in select_statements) == 1


def test_snapshot_document_decodes_payload_once(database, monkeypatch) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Single decode")
    snapshots = SnapshotService(database)
    saved = snapshots.create(campaign.id, label="One payload")
    original_decode = snapshot_module.decode_snapshot_payload
    calls = 0

    def counted_decode(**kwargs):
        nonlocal calls
        calls += 1
        return original_decode(**kwargs)

    monkeypatch.setattr(snapshot_module, "decode_snapshot_payload", counted_decode)
    document = snapshots.get(campaign.id, saved.slot)

    assert document["valid"] is True
    assert calls == 1


def test_state_revisions_share_compressed_documents_and_fail_closed(database) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Shared revision documents",
        state={"clock": 1},
    )
    revisions = RevisionService(database)
    change = {
        "operation": "test.shared-document",
        "entity_type": "campaign",
        "entity_id": campaign.id,
        "before": {"state": {"clock": 0}, "revision": 1},
        "after": {"state": {"clock": 1}, "revision": 2},
    }
    revisions.record(campaign.id, **change)
    revisions.record(campaign.id, **change)

    with database.transaction() as session:
        rows = list(
            session.scalars(
                select(StateRevision)
                .where(StateRevision.campaign_id == campaign.id)
                .order_by(StateRevision.sequence)
            )
        )
        documents = list(session.scalars(select(StateDocument)))
        assert len(documents) == 2
        assert rows[0].before_document_id == rows[1].before_document_id
        assert rows[0].after_document_id == rows[1].after_document_id
        session.get(StateDocument, rows[-1].before_document_id).compressed_payload += b"trailing"

    with pytest.raises(StateDocumentStorageError, match="decompressed size"):
        revisions.undo(campaign.id)
    assert RevisionService(database).history(campaign.id)[0].applied is True


def test_snapshot_storage_rejects_trailing_data_and_oversized_decompression() -> None:
    identity = {
        "schema_version": 8,
        "snapshot_id": "snapshot-1",
        "campaign_id": "campaign-1",
        "branch_id": "branch-1",
        "parent_id": None,
        "slot": 1,
    }
    encoded = encode_snapshot_payload({"campaign": {"state": "safe"}}, **identity)
    trailing = encoded.compressed_payload + b"trailing"
    trailing_checksum = snapshot_record_checksum(
        **identity,
        payload_codec=encoded.payload_codec,
        uncompressed_size=encoded.uncompressed_size,
        payload_checksum=encoded.payload_checksum,
        compressed_payload=trailing,
    )
    with pytest.raises(SnapshotStorageError, match="decompressed size"):
        decode_snapshot_payload(
            **identity,
            payload_codec=encoded.payload_codec,
            uncompressed_size=encoded.uncompressed_size,
            payload_checksum=encoded.payload_checksum,
            record_checksum=trailing_checksum,
            compressed_payload=trailing,
        )

    truncated = encoded.compressed_payload[:-1]
    truncated_checksum = snapshot_record_checksum(
        **identity,
        payload_codec=encoded.payload_codec,
        uncompressed_size=encoded.uncompressed_size,
        payload_checksum=encoded.payload_checksum,
        compressed_payload=truncated,
    )
    with pytest.raises(SnapshotStorageError, match="decompressed size"):
        decode_snapshot_payload(
            **identity,
            payload_codec=encoded.payload_codec,
            uncompressed_size=encoded.uncompressed_size,
            payload_checksum=encoded.payload_checksum,
            record_checksum=truncated_checksum,
            compressed_payload=truncated,
        )

    garbage = b"not-a-zlib-stream"
    garbage_checksum = snapshot_record_checksum(
        **identity,
        payload_codec=encoded.payload_codec,
        uncompressed_size=encoded.uncompressed_size,
        payload_checksum=encoded.payload_checksum,
        compressed_payload=garbage,
    )
    with pytest.raises(SnapshotStorageError, match="decompression failed"):
        decode_snapshot_payload(
            **identity,
            payload_codec=encoded.payload_codec,
            uncompressed_size=encoded.uncompressed_size,
            payload_checksum=encoded.payload_checksum,
            record_checksum=garbage_checksum,
            compressed_payload=garbage,
        )

    oversized = MAX_SNAPSHOT_PAYLOAD_BYTES + 1
    oversized_checksum = snapshot_record_checksum(
        **identity,
        payload_codec=encoded.payload_codec,
        uncompressed_size=oversized,
        payload_checksum=encoded.payload_checksum,
        compressed_payload=encoded.compressed_payload,
    )
    with pytest.raises(SnapshotStorageError, match="invalid uncompressed size"):
        decode_snapshot_payload(
            **identity,
            payload_codec=encoded.payload_codec,
            uncompressed_size=oversized,
            payload_checksum=encoded.payload_checksum,
            record_checksum=oversized_checksum,
            compressed_payload=encoded.compressed_payload,
        )


def test_restore_head_recaptures_materialized_actors_and_actor_knowledge(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Restore capture")
    characters = CharacterService(database)
    actor = characters.create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Witness",
        character_type="pc",
        sheet={"hp": 7},
    )
    ActorKnowledgeService(database).add(
        campaign.id,
        actor_id=actor.id,
        knowledge_key="sealed-door",
        proposition="The eastern door is sealed.",
        disclosure_scope="owner",
    )
    snapshots = SnapshotService(database)
    saved = snapshots.create(campaign.id, label="Witness knows")
    characters.update(actor.id, sheet={"hp": 1})

    restored = snapshots.restore(campaign.id, saved.slot)
    document = snapshots.get(campaign.id, restored.slot)

    assert document["valid"] is True
    assert document["payload"]["characters"] == [
        {
            "id": actor.id,
            "system_id": "dnd5e",
            "character_type": "pc",
            "template_id": None,
            "name": "Witness",
            "player_name": None,
            "summary": "",
            "sheet": {"hp": 7},
            "notes": {},
            "revision": 1,
        }
    ]
    assert [item["knowledge_key"] for item in document["payload"]["actor_knowledge"]] == [
        "sealed-door"
    ]


def test_snapshot_head_flag_tracks_all_branch_refs_and_parent_cannot_be_forged(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="DAG heads")
    snapshots = SnapshotService(database)
    base = snapshots.create(campaign.id, label="Base")
    BranchService(database).create(
        campaign.id,
        name="still-at-base",
        from_snapshot_id=base.id,
    )
    next_save = snapshots.create(campaign.id, label="Main advances")

    heads = {item.id: item.is_head for item in snapshots.list(campaign.id)}
    assert heads == {base.id: True, next_save.id: True}
    with pytest.raises(ValueError, match="checked-out branch head"):
        snapshots.create(campaign.id, label="Forged ancestry", parent_id=base.id)


def test_branch_checkout_refuses_to_mix_unsaved_state_with_saved_continuity(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Clean checkout", state={"clock": 0})
    snapshots = SnapshotService(database)
    branches = BranchService(database)

    with pytest.raises(ValueError, match="snapshot before branching"):
        branches.create(campaign.id, name="no-baseline")

    base = snapshots.create(campaign.id, label="Clock zero")
    main = branches.current(campaign.id)
    alternate = branches.create(
        campaign.id,
        name="clock-zero-copy",
        from_snapshot_id=base.id,
    )
    campaigns.update(campaign.id, state={"clock": 1})

    with pytest.raises(ValueError, match="unsaved changes"):
        snapshots.checkout_branch(campaign.id, alternate.id)
    assert branches.current(campaign.id).id == main.id
    assert campaigns.get(campaign.id).state == {"clock": 1}

    snapshots.create(campaign.id, label="Clock one")
    snapshots.checkout_branch(campaign.id, alternate.id)
    assert campaigns.get(campaign.id).state == {"clock": 0}


def test_branch_checkout_bulk_restores_large_revision_cursor(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Bulk cursor", state={"step": -1})
    mutations = StateMutationService(database)
    snapshots = SnapshotService(database)
    branches = BranchService(database)
    main_branch_id = branches.current(campaign.id).id

    for step in range(60):
        current = campaigns.get(campaign.id)
        mutations.replace(
            campaign.id,
            campaign_state={"step": step},
            expected_campaign_revision=current.revision,
            operation="test.bulk-cursor",
            idempotency_key=f"bulk-cursor-{step}",
        )
    base = snapshots.create(campaign.id, label="Sixty revisions")
    copy_statements: list[str] = []

    def record_copy_statement(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        copy_statements.append(" ".join(statement.upper().split()))

    event.listen(database.engine, "before_cursor_execute", record_copy_statement)
    try:
        alternate = branches.create(
            campaign.id,
            name="bulk-cursor-copy",
            from_snapshot_id=base.id,
        )
    finally:
        event.remove(
            database.engine,
            "before_cursor_execute",
            record_copy_statement,
        )
    revision_copy_selects = [
        statement
        for statement in copy_statements
        if statement.startswith("SELECT") and "FROM STATE_REVISIONS" in statement
    ]
    assert revision_copy_selects
    assert all("COMPRESSED_PAYLOAD" not in statement for statement in revision_copy_selects)
    with database.session_factory() as session:
        cloned_revisions = list(
            session.scalars(
                select(StateRevision)
                .join(
                    MutationGroup,
                    MutationGroup.id == StateRevision.mutation_group_id,
                )
                .where(MutationGroup.branch_id == alternate.id)
                .order_by(StateRevision.sequence)
            )
        )
        source_revisions = list(
            session.scalars(
                select(StateRevision)
                .join(
                    MutationGroup,
                    MutationGroup.id == StateRevision.mutation_group_id,
                )
                .where(MutationGroup.branch_id == main_branch_id)
                .order_by(StateRevision.sequence)
            )
        )
        audit_rows = list(session.scalars(select(AuditLog)))
    assert len(cloned_revisions) == 60
    assert len(source_revisions) == 60
    assert [
        (row.before_document_id, row.after_document_id) for row in cloned_revisions
    ] == [
        (row.before_document_id, row.after_document_id) for row in source_revisions
    ]
    assert audit_rows
    assert all(row.before is None and row.after is None for row in audit_rows)
    current = campaigns.get(campaign.id)
    mutations.replace(
        campaign.id,
        campaign_state={"step": 61},
        expected_campaign_revision=current.revision,
        operation="test.bulk-cursor",
        idempotency_key="bulk-cursor-main-only",
    )
    snapshots.create(campaign.id, label="Main advances")

    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record_statement)
    try:
        snapshots.checkout_branch(campaign.id, alternate.id)
    finally:
        event.remove(database.engine, "before_cursor_execute", record_statement)

    assert campaigns.get(campaign.id).state == {"step": 59}
    RevisionService(database).undo(campaign.id)
    assert campaigns.get(campaign.id).state == {"step": 58}
    revision_statements = [
        statement for statement in statements if "state_revisions" in statement.casefold()
    ]
    assert len(revision_statements) <= 12


def test_state_mutation_replaces_campaign_and_character_documents_atomically(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Mutations")
    characters = CharacterService(database)
    hero = characters.create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Mira",
        sheet={"wallet": {"gp": 1}},
        notes={"memories": []},
    )

    StateMutationService(database).replace(
        campaign.id,
        campaign_state={"party": {"wallet": {"gp": 2}}},
        character_updates=[
            CharacterStateUpdate(
                character_id=hero.id,
                expected_revision=hero.revision,
                sheet={"wallet": {"gp": 0}},
                notes={"memories": [{"summary": "Paid the party fund."}]},
            )
        ],
    )

    assert CampaignService(database).get(campaign.id).state["party"]["wallet"] == {"gp": 2}

    updated = characters.get(hero.id)
    assert updated.sheet["wallet"] == {"gp": 0}
    assert updated.notes["memories"][0]["summary"] == "Paid the party fund."

    with pytest.raises(ValueError, match="campaign revision conflict"):
        StateMutationService(database).replace(
            campaign.id,
            campaign_state={"party": {"wallet": {"gp": 3}}},
            expected_campaign_revision=0,
        )
    assert CampaignService(database).get(campaign.id).state["party"]["wallet"] == {"gp": 2}

    with pytest.raises(ValueError):
        StateMutationService(database).replace(
            campaign.id,
            campaign_state={"party": {"wallet": {"gp": 99}}},
            character_updates=[
                CharacterStateUpdate(
                    character_id=hero.id,
                    expected_revision=hero.revision,
                    sheet={},
                    notes={},
                )
            ],
        )

    assert CampaignService(database).get(campaign.id).state["party"]["wallet"] == {"gp": 2}


def test_state_mutation_atomically_transfers_complete_actor_knowledge(database) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Body Thief knowledge",
        state={"phase": "before"},
    )
    characters = CharacterService(database)
    target = characters.create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Target",
        sheet={},
        notes={},
    )
    devourer = characters.create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Intellect Devourer",
        sheet={},
        notes={},
    )
    knowledge = ActorKnowledgeService(database)
    known = knowledge.add(
        campaign.id,
        actor_id=target.id,
        knowledge_key="vault-key",
        proposition="The vault key is hidden under the third flagstone.",
        subject_ref="vault",
        epistemic_status="known",
        confidence=3,
        cause="witnessed",
        disclosure_scope="owner",
    )

    StateMutationService(database).replace(
        campaign.id,
        campaign_state={"phase": "body-taken"},
        actor_knowledge_transfers=[
            ActorKnowledgeTransfer(
                source_actor_id=target.id,
                destination_actor_id=devourer.id,
                knowledge_key_prefix=f"body-thief.{target.id}",
            )
        ],
        expected_campaign_revision=campaign.revision,
        operation="combat.activity.source_contest_effect",
        idempotency_key="body-thief-knowledge",
    )

    target_after = knowledge.list(campaign.id, actor_id=target.id)
    copied = knowledge.list(campaign.id, actor_id=devourer.id)
    assert [item.id for item in target_after] == [known.id]
    assert len(copied) == 1
    assert copied[0].knowledge_key == f"body-thief.{target.id}.{known.id}"
    assert copied[0].proposition == known.proposition
    assert copied[0].subject_ref == known.subject_ref
    assert copied[0].cause == "body_thief"
    assert copied[0].disclosure_scope == "dm"
    assert CampaignService(database).get(campaign.id).state == {"phase": "body-taken"}
    with pytest.raises(ValueError, match="restore a snapshot or branch"):
        RevisionService(database).undo(campaign.id)


def test_state_mutation_transfers_only_explicit_actor_knowledge_ids(database) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Bounded knowledge transfer",
        state={"phase": "before"},
    )
    characters = CharacterService(database)
    source = characters.create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Source",
        sheet={},
        notes={},
    )
    destination = characters.create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Destination",
        sheet={},
        notes={},
    )
    knowledge = ActorKnowledgeService(database)
    shared = knowledge.add(
        campaign.id,
        actor_id=source.id,
        knowledge_key="shared-clue",
        proposition="The red seal opens the public archive.",
    )
    private = knowledge.add(
        campaign.id,
        actor_id=source.id,
        knowledge_key="private-clue",
        proposition="The source secretly betrayed the archivist.",
    )

    StateMutationService(database).replace(
        campaign.id,
        campaign_state={"phase": "shared"},
        actor_knowledge_transfers=[
            ActorKnowledgeTransfer(
                source_actor_id=source.id,
                destination_actor_id=destination.id,
                knowledge_key_prefix="told",
                knowledge_ids=(shared.id,),
                cause="told_by",
            )
        ],
        expected_campaign_revision=campaign.revision,
        operation="knowledge.transfer.selected",
        idempotency_key="knowledge-transfer-selected",
    )

    copied = knowledge.list(campaign.id, actor_id=destination.id)
    assert [item.proposition for item in copied] == [shared.proposition]
    assert private.proposition not in {item.proposition for item in copied}

    current = CampaignService(database).get(campaign.id)
    with pytest.raises(ValueError, match="current active knowledge"):
        StateMutationService(database).replace(
            campaign.id,
            campaign_state={"phase": "invalid"},
            actor_knowledge_transfers=[
                ActorKnowledgeTransfer(
                    source_actor_id=source.id,
                    destination_actor_id=destination.id,
                    knowledge_key_prefix="invalid",
                    knowledge_ids=("not-owned",),
                )
            ],
            expected_campaign_revision=current.revision,
            operation="knowledge.transfer.invalid",
            idempotency_key="knowledge-transfer-invalid",
        )
    assert CampaignService(database).get(campaign.id).state == {"phase": "shared"}


def test_state_mutation_exposes_committed_idempotency_recovery_without_a_receipt(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Receipt recovery")
    public_request = {
        "operation": "test.receipt.recovery",
        "campaign_id": campaign.id,
        "phase": "after",
    }
    StateMutationService(database).replace(
        campaign.id,
        campaign_state={"phase": "after"},
        operation="test.receipt.recovery",
        idempotency_key="recover-on-retry",
        idempotency_request_hash=request_hash(public_request),
    )
    idempotency = IdempotencyService(database)
    assert idempotency.mutation_committed(
        campaign.id,
        "recover-on-retry",
        public_request,
    )
    recovered = idempotency.receipt(campaign.id, "recover-on-retry")
    assert recovered.request_hash == request_hash(public_request)
    assert recovered.response == {
        "status": "committed",
        "idempotency_replayed": True,
        "response_recovery": "read_current_state",
    }
    assert recovered.entity_revisions == [
        {
            "entity_type": "campaign",
            "entity_id": campaign.id,
            "before_revision": campaign.revision,
            "after_revision": campaign.revision + 1,
        }
    ]
    with pytest.raises(ValueError, match="different request"):
        idempotency.mutation_committed(
            campaign.id,
            "recover-on-retry",
            {**public_request, "phase": "different"},
        )
    with pytest.raises(ValueError, match="committed mutation group"):
        StateMutationService(database).replace(
            campaign.id,
            campaign_state={"phase": "duplicated"},
            operation="test.receipt.recovery",
            idempotency_key="recover-on-retry",
        )
    assert CampaignService(database).get(campaign.id).state == {"phase": "after"}


def test_state_mutation_persists_exact_replay_response_atomically(database) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Atomic replay",
        state={"phase": "before"},
    )
    branch_id = BranchService(database).current(campaign.id).id
    public_request = {"phase": "after", "branch_id": branch_id}
    revisions = StateMutationService(database).replace(
        campaign.id,
        campaign_state={"phase": "after"},
        expected_campaign_revision=campaign.revision,
        operation="test.atomic-replay",
        idempotency_key="atomic-replay",
        idempotency_write=IdempotencyWrite(
            scope=(f"test-atomic-replay:{campaign.id}:{branch_id}:system:local"),
            payload=public_request,
            response=lambda committed: {
                "status": "committed",
                "campaign_revision": campaign.revision + 1,
                "revisions": [item.sequence for item in committed],
            },
        ),
    )

    receipt = IdempotencyService(database).receipt(campaign.id, "atomic-replay")
    assert receipt.response == {
        "status": "committed",
        "campaign_revision": campaign.revision + 1,
        "revisions": [item.sequence for item in revisions],
    }
    assert receipt.request_hash == request_hash(public_request)
    assert receipt.mutation_group_id == revisions[0].mutation_group_id
    history = RevisionService(database).history(campaign.id)
    assert history[0].idempotency_key == "atomic-replay"
    assert history[0].request_hash == request_hash(public_request)


def test_state_mutation_rolls_back_when_atomic_replay_response_cannot_be_built(
    database,
) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Atomic replay rollback",
        state={"phase": "before"},
    )

    def fail_response(_revisions):
        raise RuntimeError("response serialization failed")

    with pytest.raises(RuntimeError, match="response serialization failed"):
        StateMutationService(database).replace(
            campaign.id,
            campaign_state={"phase": "must-roll-back"},
            expected_campaign_revision=campaign.revision,
            operation="test.atomic-replay-rollback",
            idempotency_key="atomic-replay-rollback",
            idempotency_write=IdempotencyWrite(
                scope=f"test-atomic-replay:{campaign.id}",
                payload={"phase": "must-roll-back"},
                response=fail_response,
            ),
        )

    assert CampaignService(database).get(campaign.id).state == {"phase": "before"}
    with pytest.raises(LookupError, match="receipt not found"):
        IdempotencyService(database).receipt(
            campaign.id,
            "atomic-replay-rollback",
        )


def test_continuity_writes_share_atomic_idempotency_and_rollback(database) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Atomic continuity replay",
    )
    actor = CharacterService(database).create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Witness",
        character_type="npc",
    )
    memories = MemoryService(database)
    events = EventService(database)
    knowledge = ActorKnowledgeService(database)
    idempotency = IdempotencyService(database)
    scope = f"memory-change:add:{campaign.id}:system:local"
    payload = {"fact_key": "door", "content": "The door is locked."}

    fact = memories.add(
        campaign.id,
        fact_key="door",
        content="The door is locked.",
        idempotency_key="atomic-fact",
        idempotency_write=IdempotencyWrite(
            scope=scope,
            payload=payload,
            response=lambda result: {"id": result.id, "revision_id": result.revision_id},
        ),
    )
    replay = idempotency.lookup(scope, "atomic-fact", payload)
    assert replay is not None
    assert replay.response == {"id": fact.id, "revision_id": fact.revision_id}
    with pytest.raises(ValueError, match="committed response"):
        memories.add(
            campaign.id,
            fact_key="door-copy",
            content="This duplicate must not commit.",
            idempotency_key="atomic-fact",
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=payload,
                response=lambda result: {"id": result.id},
            ),
        )
    assert [item.fact_key for item in memories.list(campaign.id)] == ["door"]

    def fail_response(_result):
        raise RuntimeError("continuity response serialization failed")

    with pytest.raises(RuntimeError, match="continuity response serialization failed"):
        events.add(
            campaign.id,
            summary="This event must roll back.",
            idempotency_key="failed-event",
            idempotency_write=IdempotencyWrite(
                scope=f"event-add:{campaign.id}:system:local",
                payload={"summary": "This event must roll back."},
                response=fail_response,
            ),
        )
    assert events.list(campaign.id) == []

    with pytest.raises(RuntimeError, match="continuity response serialization failed"):
        knowledge.add(
            campaign.id,
            actor_id=actor.id,
            knowledge_key="failed-knowledge",
            proposition="This knowledge must roll back.",
            idempotency_key="failed-knowledge",
            idempotency_write=IdempotencyWrite(
                scope=f"actor-knowledge:{campaign.id}:{actor.id}",
                payload={"knowledge_key": "failed-knowledge"},
                response=fail_response,
            ),
        )
    assert knowledge.list(campaign.id, actor_id=actor.id) == []

    with pytest.raises(RuntimeError, match="continuity response serialization failed"):
        ContinuityCommitService(database).commit(
            campaign.id,
            event={"summary": "The whole scene commit must roll back."},
            facts=[
                {
                    "action": "add",
                    "fact_key": "failed-scene-fact",
                    "content": "This fact must roll back.",
                }
            ],
            idempotency_key="failed-continuity-commit",
            idempotency_write=IdempotencyWrite(
                scope=f"continuity-commit:{campaign.id}:system:local",
                payload={"scene": "failed"},
                response=fail_response,
            ),
        )
    assert events.list(campaign.id) == []
    assert [item.fact_key for item in memories.list(campaign.id)] == ["door"]


def test_snapshot_recap_query_is_pure_and_checkpoint_receipt_is_atomic(database) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Pure recap and atomic checkpoint",
        state={"scene": "opening"},
    )
    snapshots = SnapshotService(database)

    def fail_response(_result):
        raise RuntimeError("snapshot response serialization failed")

    with pytest.raises(RuntimeError, match="snapshot response serialization failed"):
        snapshots.create(
            campaign.id,
            label="Must roll back",
            idempotency_key="failed-checkpoint",
            idempotency_write=IdempotencyWrite(
                scope=f"snapshot-create:{campaign.id}:system:local",
                payload={"label": "Must roll back"},
                response=fail_response,
            ),
        )
    assert snapshots.list(campaign.id) == []

    saved = snapshots.create(campaign.id, label="Opening")
    with database.transaction() as session:
        row = session.get(CampaignSnapshot, saved.id)
        assert row is not None
        row.recap = {"source": "sentinel", "summary": "Presentation-owned text"}
    computed = snapshots.regenerate_recap(campaign.id, saved.slot)
    assert computed["source"] == "deterministic"
    assert snapshots.get(campaign.id, saved.slot)["recap"] == {
        "source": "sentinel",
        "summary": "Presentation-owned text",
    }
    original_branch = BranchService(database).current(campaign.id)
    with pytest.raises(RuntimeError, match="snapshot response serialization failed"):
        BranchService(database).create(
            campaign.id,
            name="must-roll-back",
            from_snapshot_id=saved.id,
            checkout=True,
            idempotency_key="failed-branch",
            idempotency_write=IdempotencyWrite(
                scope=f"branch-create:{campaign.id}:system:local",
                payload={"name": "must-roll-back"},
                response=fail_response,
            ),
        )
    assert [item.id for item in BranchService(database).list(campaign.id)] == [original_branch.id]
    assert BranchService(database).current(campaign.id).id == original_branch.id


def test_undo_rolls_back_when_its_replay_receipt_cannot_be_built(database) -> None:
    campaign = CampaignService(database).create(
        system_id="dnd5e",
        name="Atomic undo",
        state={"phase": "before"},
    )
    StateMutationService(database).replace(
        campaign.id,
        campaign_state={"phase": "after"},
        operation="test.atomic-undo",
    )

    def fail_response(_result):
        raise RuntimeError("undo response serialization failed")

    with pytest.raises(RuntimeError, match="undo response serialization failed"):
        RevisionService(database).undo(
            campaign.id,
            idempotency_key="failed-undo",
            idempotency_write=IdempotencyWrite(
                scope=f"state-undo:{campaign.id}:system:local",
                payload={"operation": "undo"},
                response=fail_response,
            ),
        )
    assert CampaignService(database).get(campaign.id).state == {"phase": "after"}
    assert RevisionService(database).history(campaign.id)[0].applied is True


def test_audited_campaign_update_and_exact_replay_receipt_share_one_transaction(
    database,
) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(
        system_id="dnd5e",
        name="Atomic campaign update",
        state={"phase": "before"},
    )
    payload = {"state": {"phase": "after"}}
    response = campaigns.update_audited(
        campaign.id,
        state={"phase": "after"},
        expected_revision=campaign.revision,
        idempotency_key="campaign-update",
        idempotency_write=IdempotencyWrite(
            scope=f"campaign-update:{campaign.id}:system:local",
            payload=payload,
            response=lambda result: {
                "campaign_id": result.id,
                "revision": result.revision,
                "state": result.state,
            },
        ),
    )
    replay = IdempotencyService(database).lookup(
        f"campaign-update:{campaign.id}:system:local",
        "campaign-update",
        payload,
    )
    assert replay is not None
    assert replay.response == {
        "campaign_id": campaign.id,
        "revision": response.revision,
        "state": {"phase": "after"},
    }

    def fail_response(_result):
        raise RuntimeError("campaign response serialization failed")

    with pytest.raises(RuntimeError, match="campaign response serialization failed"):
        campaigns.update_audited(
            campaign.id,
            state={"phase": "must-roll-back"},
            expected_revision=response.revision,
            idempotency_key="failed-campaign-update",
            idempotency_write=IdempotencyWrite(
                scope=f"campaign-update:{campaign.id}:system:local",
                payload={"state": {"phase": "must-roll-back"}},
                response=fail_response,
            ),
        )
    persisted = campaigns.get(campaign.id)
    assert persisted.revision == response.revision
    assert persisted.state == {"phase": "after"}


def test_state_mutation_persists_rule_receipts_in_the_same_group(database) -> None:
    campaign = CampaignService(database).create(system_id="dnd5e", name="Rule receipts")
    revisions = StateMutationService(database).replace(
        campaign.id,
        campaign_state={"phase": "resolved"},
        operation="test.rule.receipt",
        rule_receipts=[
            {
                "mechanic_id": "dnd5e.core.activity.accounting",
                "event": "activity.after",
                "operations": [],
                "citations": [{"source": "SRD", "section": "Actions"}],
                "ruleset_fingerprint": "a" * 64,
            }
        ],
    )

    receipts = RuleReceiptService(database).list(campaign.id)
    assert len(receipts) == 1
    assert receipts[0].mutation_group_id == revisions[0].mutation_group_id
    assert receipts[0].branch_id is not None
    assert receipts[0].mechanic_id == "dnd5e.core.activity.accounting"
    assert receipts[0].receipt["citations"][0]["section"] == "Actions"
    assert receipts[0].operation == "test.rule.receipt"
    assert receipts[0].applied is True

    snapshot = SnapshotService(database).create(campaign.id, label="After settlement")
    fork = BranchService(database).create(
        campaign.id,
        name="receipt-fork",
        from_snapshot_id=snapshot.id,
    )
    fork_receipts = RuleReceiptService(database).list(campaign.id, branch_id=fork.id)
    assert len(fork_receipts) == 1
    assert fork_receipts[0].branch_id == fork.id
    assert fork_receipts[0].mutation_group_id != receipts[0].mutation_group_id
    assert fork_receipts[0].receipt == receipts[0].receipt

    RevisionService(database).undo(campaign.id)
    assert (
        RuleReceiptService(database).list(campaign.id, branch_id=receipts[0].branch_id)[0].applied
        is False
    )
    assert RuleReceiptService(database).list(campaign.id, branch_id=fork.id)[0].applied is True


def test_continuity_commit_coordinates_state_progress_receipts_and_rollback(database) -> None:
    campaign = CampaignService(database).create(system_id="neutral", name="Full settlement")
    characters = CharacterService(database)
    actor = characters.create(
        system_id="neutral",
        campaign_id=campaign.id,
        name="Mira",
        sheet={"resolve": 1},
    )
    modules = ModuleService(database)
    modules.ingest(
        campaign_id=campaign.id,
        source_key="settlement.md",
        title="Settlement",
        content="# Chapter\n## Scene\nA choice changes the road.",
    )
    scene_id = modules.scene_index(campaign.id)[0]["scene_id"]
    result = ContinuityCommitService(database).commit(
        campaign.id,
        expected_campaign_revision=campaign.revision,
        campaign_state={"chapter": "two"},
        character_updates=[
            CharacterStateUpdate(
                character_id=actor.id,
                expected_revision=actor.revision,
                sheet={"resolve": 2},
                notes={},
            )
        ],
        scene_progress_updates=[
            {"scene_id": scene_id, "progress": 50, "expected_state_version": 0}
        ],
        event={"event_type": "choice", "summary": "The road changes."},
        facts=[{"fact_key": "road.changed", "content": "The road changed."}],
        rule_receipts=[
            {
                "ruleset_fingerprint": "neutral-profile",
                "mechanic_id": "choice.resolve",
                "event": "choice",
            }
        ],
    )
    assert result["campaign_revision"] == campaign.revision + 1
    assert CampaignService(database).get(campaign.id).state == {"chapter": "two"}
    assert characters.get(actor.id).sheet == {"resolve": 2}
    assert modules.current_scene(campaign.id)["progress"]["percent"] == 50
    assert RuleReceiptService(database).list(campaign.id)[0].mechanic_id == "choice.resolve"
    with pytest.raises(ValueError, match="restore a snapshot or branch"):
        RevisionService(database).undo(campaign.id)
    assert CampaignService(database).get(campaign.id).state == {"chapter": "two"}
    assert len(EventService(database).list(campaign.id)) == 1
    assert MemoryService(database).list(campaign.id)[0].fact_key == "road.changed"

    current = CampaignService(database).get(campaign.id)
    current_actor = characters.get(actor.id)
    with pytest.raises(ValueError, match="content is required"):
        ContinuityCommitService(database).commit(
            campaign.id,
            expected_campaign_revision=current.revision,
            campaign_state={"chapter": "corrupt"},
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor.id,
                    expected_revision=current_actor.revision,
                    sheet={"resolve": 99},
                    notes={},
                )
            ],
            scene_progress_updates=[
                {"scene_id": scene_id, "progress": 99, "expected_state_version": 1}
            ],
            event={"event_type": "broken", "summary": "This must roll back."},
            facts=[{"fact_key": "broken.fact"}],
        )
    assert CampaignService(database).get(campaign.id).state == {"chapter": "two"}
    assert characters.get(actor.id).sheet == {"resolve": 2}
    assert modules.current_scene(campaign.id)["progress"]["percent"] == 50


def test_restore_and_checkout_enforce_campaign_head_preconditions(database) -> None:
    campaigns = CampaignService(database)
    campaign = campaigns.create(system_id="neutral", name="Recovery CAS")
    snapshots = SnapshotService(database)
    snapshot = snapshots.create(campaign.id, label="base")
    branch = BranchService(database).create(campaign.id, name="alternate")
    stale = campaigns.get(campaign.id)
    campaigns.update(campaign.id, state={"newer": True}, expected_revision=stale.revision)

    with pytest.raises(ValueError, match="campaign revision conflict"):
        snapshots.restore(
            campaign.id,
            snapshot.slot,
            expected_revision=stale.revision,
            expected_branch_id=BranchService(database).current(campaign.id).id,
        )
    with pytest.raises(ValueError, match="campaign revision conflict"):
        BranchService(database).checkout(
            campaign.id,
            branch.id,
            expected_revision=stale.revision,
            expected_branch_id=BranchService(database).current(campaign.id).id,
        )
    assert campaigns.get(campaign.id).state == {"newer": True}


def test_pdf_normalization_and_module_generator_structure(database) -> None:
    content, stats, warnings = build_structured_markdown(
        [
            "Book Header\n目录\n第一章：目录项\n1",
            "Book Header\n第一章 正文\nChapter 1\n运作本章\n正文。\nA1. Gate\n房间。\n2",
        ],
        [DocumentBookmark("运作本章", 2, 0)],
    )
    assert "Book Header" not in content
    assert "<!-- page: 2 -->" in content
    assert stats["matched_bookmarks"] == 1
    assert not warnings

    campaign = CampaignService(database).create(system_id="dnd5e", name="Generated")
    result = ModuleService(database).ingest(
        campaign_id=campaign.id,
        source_key="generated.md",
        title="Generated",
        content=(
            "# 第一章\n"
            "## 酒馆\n线索出现。\n"
            "### 遭遇\n敌人靠近。\n"
            "#### A1. 地窖\n门后有宝箱。\n"
            "## 广场\n群众聚集。\n"
            "# 附录\n"
            "## NPC\n| 姓名 | 目标 |\n|---|---|\n| 米拉 | 逃离 |\n"
        ),
    )
    assert result.chapters == 2
    assert result.scenes >= 3
    hit = ModuleService(database).search(campaign_id=campaign.id, query="宝箱")[0]
    assert hit.title == "酒馆"
