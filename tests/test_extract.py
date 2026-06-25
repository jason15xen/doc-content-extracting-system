import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.main import app
from app.settings import get_settings

FIXTURES = Path(__file__).parent / "fixtures"
client = TestClient(app)

HAS_SOFFICE = shutil.which("soffice") is not None


@pytest.fixture(autouse=True)
def _override_pipeline_ctx():
    """The /extract route depends on the pipeline context (for OCR settings).
    The module-level TestClient doesn't run lifespan, so `app.state.pipeline`
    is never populated — provide a lightweight stub so the route works in tests
    without booting Azure/OCR."""

    class _Ctx:
        settings = get_settings()
        ocr_pool = None

    app.dependency_overrides[deps.get_pipeline_context] = lambda: _Ctx()
    yield
    app.dependency_overrides.pop(deps.get_pipeline_context, None)


def _post_one(path: Path):
    with open(path, "rb") as f:
        return client.post("/extract", files=[("files", (path.name, f))])


def _post_many(paths: list[Path]):
    handles = [open(p, "rb") for p in paths]
    try:
        return client.post(
            "/extract",
            files=[("files", (p.name, h)) for p, h in zip(paths, handles)],
        )
    finally:
        for h in handles:
            h.close()


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_unsupported_extension_returns_200_with_error():
    """Bad file returns a per-file error entry, not a 4xx status."""
    r = client.post("/extract", files=[("files", ("x.zip", b"not a doc"))])
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["filename"] == "x.zip"
    assert "Unsupported" in body[0]["error"]
    assert "plain_text" not in body[0]
    assert "file_type" not in body[0]


def test_single_file_success_shape():
    r = _post_one(FIXTURES / "sample.txt")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert set(body[0].keys()) == {"filename", "file_type", "plain_text"}


def test_multiple_files_returns_matching_list():
    paths = [FIXTURES / "sample.txt", FIXTURES / "sample.md", FIXTURES / "sample.docx"]
    r = _post_many(paths)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 3
    assert [item["filename"] for item in body] == ["sample.txt", "sample.md", "sample.docx"]
    assert all("error" not in item for item in body)


def test_partial_success_one_bad_one_good():
    """The bad file fails; the good file still succeeds."""
    with open(FIXTURES / "sample.txt", "rb") as good:
        r = client.post(
            "/extract",
            files=[
                ("files", ("bad.zip", b"nope")),
                ("files", ("sample.txt", good)),
            ],
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 2

    # First item: error entry for bad.zip
    assert body[0]["filename"] == "bad.zip"
    assert "Unsupported" in body[0]["error"]

    # Second item: successful extraction of sample.txt
    assert body[1]["filename"] == "sample.txt"
    assert "Plain text file" in body[1]["plain_text"]
    assert "error" not in body[1]


def test_all_files_bad_still_returns_200_list():
    r = client.post(
        "/extract",
        files=[
            ("files", ("a.zip", b"nope")),
            ("files", ("b.exe", b"nope")),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert all("error" in item for item in body)


@pytest.mark.parametrize(
    "name,expected_substr",
    [
        ("sample.docx", "Hello docx world"),
        ("sample.docm", "Hello docx world"),
        ("sample.xlsx", "alpha"),
        ("sample.xlsm", "alpha"),
        ("sample.pptx", "Slide title"),
        ("sample.pptm", "Slide title"),
        ("sample.pdf", "Hello pdf world"),
        ("sample.txt", "Plain text file"),
        ("sample.md", "Heading"),
    ],
)
def test_openxml_and_other(name, expected_substr):
    path = FIXTURES / name
    r = _post_one(path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["filename"] == name
    assert expected_substr in body[0]["plain_text"]


def test_pdf_multi_column_reading_order():
    """Left column must be fully emitted before right column starts."""
    r = _post_one(FIXTURES / "sample_two_col.pdf")
    assert r.status_code == 200, r.text
    text = r.json()[0]["plain_text"]
    left_end = text.find("LEFTEND")
    right_start = text.find("RIGHTSTART")
    assert left_end != -1 and right_start != -1, f"markers missing: {text!r}"
    assert left_end < right_start, (
        f"columns interleaved: LEFTEND@{left_end} RIGHTSTART@{right_start}"
    )


def _build_pdf(path: Path, pages: list[tuple[str, str | None]]) -> None:
    """Build a PDF where each page is either ('text', <string>) — a real text
    layer — or ('image', None) — a full-page raster with no text, i.e. what
    scan detection treats as a scanned page."""
    import pymupdf

    doc = pymupdf.open()
    try:
        for kind, payload in pages:
            page = doc.new_page()
            if kind == "text":
                page.insert_text((72, 72), payload or "")
            else:
                rect = page.rect
                pix = pymupdf.Pixmap(
                    pymupdf.csRGB,
                    pymupdf.IRect(0, 0, int(rect.width), int(rect.height)),
                )
                pix.clear_with(220)
                page.insert_image(rect, pixmap=pix)
        doc.save(str(path))
    finally:
        doc.close()


def test_scanned_page_skipped_and_noted(tmp_path):
    """With OCR off (default), a scanned page is dropped, the text page is
    still extracted, and the skipped page index is surfaced."""
    from app.extraction.extractors.pdf import PymupdfExtractor

    pdf = tmp_path / "mixed.pdf"
    _build_pdf(
        pdf,
        [
            ("text", "Hello text page with well over fifty characters of real content here."),
            ("image", None),
        ],
    )
    ext = PymupdfExtractor(file_type="pdf")  # ocr_pool=None, reject_scanned=False
    result = ext.extract(str(pdf), "mixed.pdf")
    assert "Hello text page" in result["plain_text"]
    assert result["skipped_pages"] == [2]


def test_all_scanned_pdf_yields_empty_text(tmp_path):
    """A fully-scanned PDF with no text layer produces no text (the pipeline
    turns this into a FAILED doc) while still tracking every skipped page."""
    from app.extraction.extractors.pdf import PymupdfExtractor

    pdf = tmp_path / "scanned.pdf"
    _build_pdf(pdf, [("image", None), ("image", None)])
    ext = PymupdfExtractor(file_type="pdf")
    result = ext.extract(str(pdf), "scanned.pdf")
    assert result["plain_text"] == ""
    assert result["skipped_pages"] == [1, 2]
    assert result["scanned_pages"] == [1, 2]


def test_scanned_page_with_text_layer_is_kept_and_marked(tmp_path):
    """A scanned page (full-page image) that ALSO carries a usable text layer
    (embedded OCR, or even just a footer watermark) keeps its text AND is
    marked as scanned — text is never discarded. This is the OCR_doc case."""
    import pymupdf

    from app.extraction.extractors.pdf import PymupdfExtractor

    pdf = tmp_path / "scan_with_text.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    rect = page.rect
    pix = pymupdf.Pixmap(
        pymupdf.csRGB, pymupdf.IRect(0, 0, int(rect.width), int(rect.height))
    )
    pix.clear_with(220)
    page.insert_image(rect, pixmap=pix)  # full-page raster → scanned
    page.insert_text(
        (72, 72), "Embedded OCR text layer with well over fifty characters here."
    )
    doc.save(str(pdf))
    doc.close()

    ext = PymupdfExtractor(file_type="pdf")
    result = ext.extract(str(pdf), "scan_with_text.pdf")
    assert "Embedded OCR text layer" in result["plain_text"]  # text kept
    assert result["scanned_pages"] == [1]                     # marked scanned
    assert "skipped_pages" not in result                      # nothing skipped


def _build_vector_pdf(path: Path, text: str, vector_pages: int) -> None:
    """First page is real text; the rest are dense with vector drawings and no
    text — mimicking a PDF whose body text is rendered as outlines (fonts
    converted to curves), like the Tefal manual."""
    import pymupdf

    doc = pymupdf.open()
    try:
        doc.new_page().insert_text((72, 72), text)
        for _ in range(vector_pages):
            page = doc.new_page()
            for k in range(150):  # > MIN_VECTOR_OPS distinct paths, no text
                y = 50 + k
                page.draw_line((50, y), (550, y))
        doc.save(str(path))
    finally:
        doc.close()


def test_vector_outline_page_skipped_and_noted(tmp_path):
    """A low-text page dense with vector drawings is unextractable like a scan
    (no raster image), so it is skipped and noted while the text page survives."""
    from app.extraction.extractors.pdf import PymupdfExtractor

    pdf = tmp_path / "vector.pdf"
    _build_vector_pdf(
        pdf, "Hello real text page with well over fifty characters here.", 1
    )
    ext = PymupdfExtractor(file_type="pdf")
    result = ext.extract(str(pdf), "vector.pdf")
    assert "Hello real text" in result["plain_text"]
    assert result["skipped_pages"] == [2]


def test_blank_page_not_flagged_as_skipped(tmp_path):
    """A genuinely blank page (no text, no drawings) is dropped silently — it is
    NOT reported as a skipped page, so only real content loss is flagged."""
    import pymupdf

    from app.extraction.extractors.pdf import PymupdfExtractor

    pdf = tmp_path / "blank.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text(
        (72, 72), "Enough real text on page one to clear the fifty-char gate."
    )
    doc.new_page()  # page 2: truly blank
    doc.save(str(pdf))
    doc.close()

    ext = PymupdfExtractor(file_type="pdf")
    result = ext.extract(str(pdf), "blank.pdf")
    assert "skipped_pages" not in result  # key omitted when nothing skipped


@pytest.mark.skipif(not HAS_SOFFICE, reason="soffice not available")
@pytest.mark.parametrize(
    "name,expected_substr",
    [
        ("sample.doc", "Hello docx world"),
        ("sample.xls", "alpha"),
        ("sample.ppt", "Slide title"),
    ],
)
def test_legacy(name, expected_substr):
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"{name} fixture not generated (soffice conversion failed)")
    r = _post_one(path)
    assert r.status_code == 200, r.text
    assert expected_substr in r.json()[0]["plain_text"]
