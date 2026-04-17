import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"
client = TestClient(app)

HAS_SOFFICE = shutil.which("soffice") is not None


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
