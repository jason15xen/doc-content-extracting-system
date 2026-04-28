"""Tests the top-5 distinct-doc collapse + score-max ordering logic in /search.

We swap in stub Embedder / SearchGateway / Chatter / session-dependency and drive
the endpoint via FastAPI's TestClient. No real Azure or Postgres required.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.main import app


DOC_IDS = [str(uuid.uuid4()) for _ in range(4)]


class StubEmbedder:
    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 1536 for _ in texts]

    async def aclose(self) -> None:
        pass


class StubChatter:
    async def answer(self, query: str, contexts: list[dict]) -> str:
        names = ",".join(sorted({c["doc_name"] for c in contexts}))
        return f"answer over:{names}"

    async def aclose(self) -> None:
        pass


class StubSearchGateway:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    async def hybrid_search(self, *args, **kwargs):
        return list(self._rows)

    async def aclose(self) -> None:
        pass


class StubSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class StubCtx:
    class _Settings:
        search_top_k_chunks = 30
        chat_max_context_chunks = 12

    settings = _Settings()


def _rows_for_4_docs() -> list[dict[str, Any]]:
    # Per-doc max chunk score:
    #   doc[0] = max(0.9, 0.8) = 0.9
    #   doc[1] = 1.5
    #   doc[2] = max(0.5, 0.4, 0.3) = 0.5
    #   doc[3] = 0.2
    # Ordering by score_max: B(1.5), A(0.9), C(0.5), D(0.2).
    return [
        {"doc_id": DOC_IDS[0], "doc_name": "docA.pdf", "chunk_index": 0, "content": "a0", "@search.score": 0.9},
        {"doc_id": DOC_IDS[0], "doc_name": "docA.pdf", "chunk_index": 1, "content": "a1", "@search.score": 0.8},
        {"doc_id": DOC_IDS[1], "doc_name": "docB.pdf", "chunk_index": 0, "content": "b0", "@search.score": 1.5},
        {"doc_id": DOC_IDS[2], "doc_name": "docC.pdf", "chunk_index": 0, "content": "c0", "@search.score": 0.5},
        {"doc_id": DOC_IDS[2], "doc_name": "docC.pdf", "chunk_index": 1, "content": "c1", "@search.score": 0.4},
        {"doc_id": DOC_IDS[2], "doc_name": "docC.pdf", "chunk_index": 2, "content": "c2", "@search.score": 0.3},
        {"doc_id": DOC_IDS[3], "doc_name": "docD.pdf", "chunk_index": 0, "content": "d0", "@search.score": 0.2},
    ]


@pytest.fixture
def client_with_rows():
    rows = _rows_for_4_docs()

    stub_search = StubSearchGateway(rows)
    stub_embedder = StubEmbedder()
    stub_chatter = StubChatter()

    async def _session_dep():
        yield StubSession()

    app.dependency_overrides[deps.get_session] = _session_dep
    app.dependency_overrides[deps.get_search] = lambda: stub_search
    app.dependency_overrides[deps.get_embedder] = lambda: stub_embedder
    app.dependency_overrides[deps.get_chatter] = lambda: stub_chatter
    app.dependency_overrides[deps.get_pipeline_context] = lambda: StubCtx()

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


def test_top5_collapse_and_order(client_with_rows):
    resp = client_with_rows.post("/search", json={"query": "hi", "top_k": 5})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"].startswith("answer over:")
    # Distinct docs, ordered by score_max: B(1.5), A(0.9), C(0.5), D(0.2).
    source_ids = [s["doc_id"] for s in body["sources"]]
    assert source_ids == [DOC_IDS[1], DOC_IDS[0], DOC_IDS[2], DOC_IDS[3]]

    scores = [s["score"] for s in body["sources"]]
    assert scores[0] == pytest.approx(1.5)
    assert scores[1] == pytest.approx(0.9)
    assert scores[2] == pytest.approx(0.5)
    assert scores[3] == pytest.approx(0.2)


def test_top_k_truncates(client_with_rows):
    resp = client_with_rows.post("/search", json={"query": "hi", "top_k": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert [s["doc_id"] for s in body["sources"]] == [DOC_IDS[1], DOC_IDS[0]]


def test_empty_results_skips_chat(monkeypatch):
    stub_search = StubSearchGateway([])
    stub_embedder = StubEmbedder()

    class ExplodingChatter:
        async def answer(self, *args, **kwargs):
            raise AssertionError("chat should not be called when there are no results")

        async def aclose(self):
            pass

    async def _session_dep():
        yield StubSession()

    app.dependency_overrides[deps.get_session] = _session_dep
    app.dependency_overrides[deps.get_search] = lambda: stub_search
    app.dependency_overrides[deps.get_embedder] = lambda: stub_embedder
    app.dependency_overrides[deps.get_chatter] = lambda: ExplodingChatter()
    app.dependency_overrides[deps.get_pipeline_context] = lambda: StubCtx()

    try:
        with TestClient(app) as client:
            resp = client.post("/search", json={"query": "hi"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["sources"] == []
        assert body["answer"] == "No matching documents."
    finally:
        app.dependency_overrides.clear()
