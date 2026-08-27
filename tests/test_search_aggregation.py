"""Tests the top-K distinct-doc collapse + score-max ordering logic in /query.

Drives the endpoint via FastAPI's TestClient with stubs for Embedder, SearchGateway,
Chatter and session-level repository calls. No real Azure or DB required.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.main import app
from app.repositories import datasets as datasets_repo
from app.repositories import documents as documents_repo


DOC_IDS = [str(uuid.uuid4()) for _ in range(4)]


class StubEmbedder:
    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 1536 for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        return [0.0] * 1536

    async def aclose(self) -> None:
        pass


class StubChatter:
    async def answer(self, query: str, contexts: list[dict]) -> tuple[str, dict]:
        names = ",".join(sorted({c["doc_name"] for c in contexts}))
        usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        return f"answer over:{names}", usage

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
        search_top_k_docs = 5
        chat_max_context_chunks = 12
        # Aggregation tests exercise the classic single-search path; the
        # agentic layer is covered in test_agentic_search.py.
        agentic_search_enabled = False

    settings = _Settings()


def _rows_for_4_docs() -> list[dict[str, Any]]:
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
def client_with_rows(monkeypatch):
    rows = _rows_for_4_docs()

    stub_search = StubSearchGateway(rows)
    stub_embedder = StubEmbedder()
    stub_chatter = StubChatter()

    async def _session_dep():
        yield StubSession()

    async def _bulk_get_stub(_session, _ids):
        return []

    async def _get_dataset_stub(_session, _did):
        return None

    monkeypatch.setattr(documents_repo, "bulk_get", _bulk_get_stub)
    monkeypatch.setattr(datasets_repo, "get", _get_dataset_stub)

    app.dependency_overrides[deps.get_session] = _session_dep
    app.dependency_overrides[deps.get_search] = lambda: stub_search
    app.dependency_overrides[deps.get_embedder] = lambda: stub_embedder
    app.dependency_overrides[deps.get_chatter] = lambda: stub_chatter
    app.dependency_overrides[deps.get_planner] = lambda: None
    app.dependency_overrides[deps.get_pipeline_context] = lambda: StubCtx()

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


def test_top_k_collapse_and_order(client_with_rows):
    resp = client_with_rows.post("/query", json={"query": "hi"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"].startswith("answer over:")
    # Distinct docs, ordered by score_max: B(1.5), A(0.9), C(0.5), D(0.2)
    file_ids = [f["id"] for f in body["files"]]
    assert file_ids == [DOC_IDS[1], DOC_IDS[0], DOC_IDS[2], DOC_IDS[3]]

    # relevance_score is normalized to 0-100 against the top match.
    scores = [f["relevance_score"] for f in body["files"]]
    assert scores[0] == pytest.approx(100.0)
    assert scores[1] == pytest.approx(60.0)   # 0.9/1.5 * 100
    assert scores[2] == pytest.approx(33.33, abs=0.01)
    assert scores[3] == pytest.approx(13.33, abs=0.01)


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

    async def _bulk_get_stub(_session, _ids):
        return []

    async def _get_dataset_stub(_session, _did):
        return None

    monkeypatch.setattr(documents_repo, "bulk_get", _bulk_get_stub)
    monkeypatch.setattr(datasets_repo, "get", _get_dataset_stub)

    app.dependency_overrides[deps.get_session] = _session_dep
    app.dependency_overrides[deps.get_search] = lambda: stub_search
    app.dependency_overrides[deps.get_embedder] = lambda: stub_embedder
    app.dependency_overrides[deps.get_chatter] = lambda: ExplodingChatter()
    app.dependency_overrides[deps.get_planner] = lambda: None
    app.dependency_overrides[deps.get_pipeline_context] = lambda: StubCtx()

    try:
        with TestClient(app) as client:
            resp = client.post("/query", json={"query": "hi"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["files"] == []
        assert body["answer"] == "No matching documents."
    finally:
        app.dependency_overrides.clear()
