import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"
client = TestClient(app)

HAS_SOFFICE = shutil.which("soffice") is not None


def _post(path: Path):
    with open(path, "rb") as f:
        return client.post("/extract", files={"file": (path.name, f)})


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_unsupported_extension():
    r = client.post("/extract", files={"file": ("x.zip", b"not a doc")})
    assert r.status_code == 415


def test_response_has_only_plain_text_fields():
    r = _post(FIXTURES / "sample.txt")
    assert r.status_code == 200
    assert set(r.json().keys()) == {"filename", "file_type", "plain_text"}


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
    r = _post(path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == name
    assert expected_substr in body["plain_text"]


def test_pdf_multi_column_reading_order():
    """Left column must be fully emitted before right column starts."""
    r = _post(FIXTURES / "sample_two_col.pdf")
    assert r.status_code == 200, r.text
    text = r.json()["plain_text"]
    left_end = text.find("LEFTEND")
    right_start = text.find("RIGHTSTART")
    assert left_end != -1 and right_start != -1, f"markers missing: {text!r}"
    assert left_end < right_start, (
        f"columns interleaved: LEFTEND@{left_end} RIGHTSTART@{right_start}"
    )


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
    r = _post(path)
    assert r.status_code == 200, r.text
    assert expected_substr in r.json()["plain_text"]
