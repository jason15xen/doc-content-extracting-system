import pytest

from app.services.chunker import chunk_text


def test_empty_returns_no_chunks():
    assert chunk_text("", tokens=100, overlap=10) == []
    assert chunk_text("   \n", tokens=100, overlap=10) == []


def test_short_text_one_chunk():
    out = chunk_text("hello world", tokens=100, overlap=10)
    assert len(out) == 1
    assert "hello" in out[0]


def test_long_text_has_overlap():
    # Build a large deterministic token stream.
    body = " ".join(f"tok{i}" for i in range(3000))
    chunks = chunk_text(body, tokens=200, overlap=50)
    assert len(chunks) > 2
    # Overlap check: last few tokens of chunk N should reappear at start of chunk N+1.
    for i in range(len(chunks) - 1):
        tail = chunks[i].split()[-10:]
        head = chunks[i + 1].split()[:20]
        assert any(t in head for t in tail), f"overlap not detected between chunks {i} and {i+1}"


def test_invalid_overlap_raises():
    with pytest.raises(ValueError):
        chunk_text("a b c", tokens=10, overlap=10)
    with pytest.raises(ValueError):
        chunk_text("a b c", tokens=0, overlap=0)
