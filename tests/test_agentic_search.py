"""Agentic retrieval on POST /query: search-count budget, dedupe, fallbacks.

Spec under test:
  single intent          → exactly 1 hybrid search, judge never called
  multi intent, round 1  → up to 3 sub-queries in parallel
  multi intent, round 2  → judge may add follow-ups; hard cap 5 searches total
  any planner/judge fault → degrade to the classic path, never an error
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import RateLimitError

from app import deps
from app.main import app
from app.repositories import datasets as datasets_repo
from app.repositories import documents as documents_repo
from app.services.query_planner import (
    JudgeResult,
    QueryPlan,
    parse_followups,
    parse_plan,
)
from app.services.retrieval import retrieve


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------

class Settings:
    search_top_k_chunks = 30
    search_top_k_docs = 5
    chat_max_context_chunks = 12
    agentic_search_enabled = True
    agentic_max_subqueries = 3
    agentic_max_searches = 5
    agentic_subquery_top_k = 15

    def __init__(self, **overrides: Any) -> None:
        for k, v in overrides.items():
            setattr(self, k, v)


def _row(doc: str, idx: int, score: float, chunk_id: str | None = None) -> dict[str, Any]:
    return {
        "id": chunk_id or f"{doc}-{idx}",
        "doc_id": doc,
        "doc_name": f"{doc}.pdf",
        "chunk_index": idx,
        "content": f"{doc} chunk {idx}",
        "@search.score": score,
    }


def _rate_limit_error() -> RateLimitError:
    req = httpx.Request("POST", "http://azure.test/x")
    return RateLimitError("rate limited", response=httpx.Response(429, request=req), body=None)


class CountingEmbedder:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.texts.append(text)
        return [0.0] * 8


class RecordingSearch:
    """Returns rows keyed by query text; records every call; can fail some."""

    def __init__(
        self,
        rows_for: dict[str, list[dict[str, Any]]] | None = None,
        default: list[dict[str, Any]] | None = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self.rows_for = rows_for or {}
        self.default = default if default is not None else [_row("docA", 0, 0.5)]
        self.fail_on = fail_on or set()
        self.calls: list[dict[str, Any]] = []

    async def hybrid_search(self, query, query_vector, *, top_k, dataset_id):
        self.calls.append({"query": query, "top_k": top_k, "dataset_id": dataset_id})
        if query in self.fail_on:
            raise _rate_limit_error()
        return list(self.rows_for.get(query, self.default))


class StubPlanner:
    def __init__(
        self,
        plan: QueryPlan | None = None,
        followups: list[str] | None = None,
        plan_raises: bool = False,
        judge_raises: bool = False,
        plan_usage: int = 0,
        judge_usage: int = 0,
    ) -> None:
        self._plan = plan or QueryPlan(single_intent=True)
        self._followups = followups or []
        self._plan_raises = plan_raises
        self._judge_raises = judge_raises
        self._plan_usage = plan_usage
        self._judge_usage = judge_usage
        self.plan_calls: list[dict[str, Any]] = []
        self.judge_calls: list[dict[str, Any]] = []

    async def plan(self, query, *, max_subqueries):
        self.plan_calls.append({"query": query, "max_subqueries": max_subqueries})
        if self._plan_raises:
            raise RuntimeError("planner exploded")
        p = QueryPlan(single_intent=self._plan.single_intent, subqueries=list(self._plan.subqueries))
        p.token_usage = {"prompt_tokens": self._plan_usage, "completion_tokens": 0, "total_tokens": self._plan_usage}
        return p

    async def judge(self, query, searched, snippets, *, max_followups):
        self.judge_calls.append(
            {"query": query, "searched": list(searched), "snippets": list(snippets), "max_followups": max_followups}
        )
        if self._judge_raises:
            raise RuntimeError("judge exploded")
        return JudgeResult(
            followups=list(self._followups),
            token_usage={"prompt_tokens": self._judge_usage, "completion_tokens": 0, "total_tokens": self._judge_usage},
        )


def _run(coro):
    return asyncio.run(coro)


def _retrieve(planner, search, settings=None, embedder=None, query="q"):
    return _run(
        retrieve(
            query,
            dataset_id=None,
            embedder=embedder or CountingEmbedder(),
            search_gw=search,
            planner=planner,
            settings=settings or Settings(),
        )
    )


MULTI = QueryPlan(single_intent=False, subqueries=["s1", "s2", "s3"])


# --------------------------------------------------------------------------
# Search budget
# --------------------------------------------------------------------------

def test_single_intent_runs_exactly_one_search():
    planner = StubPlanner(plan=QueryPlan(single_intent=True))
    search = RecordingSearch()
    embedder = CountingEmbedder()

    res = _retrieve(planner, search, embedder=embedder, query="what is X?")

    assert res.searches == 1
    assert res.single_intent is True
    assert [c["query"] for c in search.calls] == ["what is X?"]
    assert search.calls[0]["top_k"] == 30            # classic top_k, not the sub-query one
    assert embedder.texts == ["what is X?"]
    assert planner.judge_calls == []                 # no judge on the fast path


def test_multi_intent_runs_three_subqueries_then_judge_says_sufficient():
    planner = StubPlanner(plan=MULTI, followups=[])
    search = RecordingSearch()

    res = _retrieve(planner, search)

    assert res.searches == 3
    assert res.single_intent is False
    assert sorted(c["query"] for c in search.calls) == ["s1", "s2", "s3"]
    assert all(c["top_k"] == 15 for c in search.calls)   # per-sub-query top_k
    assert len(planner.judge_calls) == 1
    assert planner.judge_calls[0]["searched"] == ["s1", "s2", "s3"]
    assert planner.judge_calls[0]["max_followups"] == 2  # 5 - 3
    assert res.followups == []


def test_followups_bring_total_to_five_and_no_further_judging():
    planner = StubPlanner(plan=MULTI, followups=["f1", "f2"])
    search = RecordingSearch()

    res = _retrieve(planner, search)

    assert res.searches == 5
    assert [c["query"] for c in search.calls[3:]] == ["f1", "f2"]
    assert res.followups == ["f1", "f2"]
    assert len(planner.judge_calls) == 1             # exactly one judgement round


def test_hard_cap_holds_even_if_model_returns_too_many():
    # Planner bypasses parsing and hands back 5 sub-queries; judge asks for 4.
    planner = StubPlanner(
        plan=QueryPlan(single_intent=False, subqueries=["a", "b", "c", "d", "e"]),
        followups=["f1", "f2", "f3", "f4"],
    )
    search = RecordingSearch()

    res = _retrieve(planner, search)

    assert res.subqueries == ["a", "b", "c"]
    assert res.followups == ["f1", "f2"]
    assert res.searches == 5
    assert len(search.calls) == 5


def test_followups_that_repeat_a_subquery_are_dropped():
    planner = StubPlanner(plan=MULTI, followups=["S1", "f1", "s2"])
    search = RecordingSearch()

    res = _retrieve(planner, search)

    assert res.followups == ["f1"]
    assert res.searches == 4


def test_followups_are_deduped_among_themselves():
    planner = StubPlanner(plan=MULTI, followups=["f1", "F1", "f2", "f3"])
    search = RecordingSearch()

    res = _retrieve(planner, search)

    assert res.followups == ["f1", "f2"]      # repeat dropped, cap still 2
    assert res.searches == 5


def test_no_judge_call_when_budget_is_already_spent():
    planner = StubPlanner(plan=MULTI, followups=["f1"])
    search = RecordingSearch()

    res = _retrieve(planner, search, settings=Settings(agentic_max_searches=3))

    assert res.searches == 3
    assert planner.judge_calls == []
    assert res.followups == []


def test_subquery_top_k_zero_falls_back_to_classic_top_k():
    planner = StubPlanner(plan=MULTI)
    search = RecordingSearch()

    _retrieve(planner, search, settings=Settings(agentic_subquery_top_k=0))

    assert all(c["top_k"] == 30 for c in search.calls)


# --------------------------------------------------------------------------
# Merge / dedupe
# --------------------------------------------------------------------------

def test_rows_are_deduped_by_chunk_id_keeping_best_score():
    shared_low = _row("docA", 0, 0.4, chunk_id="chunk-X")
    shared_high = _row("docA", 0, 0.9, chunk_id="chunk-X")
    planner = StubPlanner(plan=MULTI)
    search = RecordingSearch(
        rows_for={
            "s1": [shared_low, _row("docB", 0, 0.3)],
            "s2": [shared_high, _row("docC", 0, 0.7)],
            "s3": [_row("docD", 0, 0.1)],
        }
    )

    res = _retrieve(planner, search)

    ids = [r["id"] for r in res.rows]
    assert ids.count("chunk-X") == 1
    assert next(r for r in res.rows if r["id"] == "chunk-X")["@search.score"] == 0.9
    # Merged rows come back best-first.
    assert [r["@search.score"] for r in res.rows] == [0.9, 0.7, 0.3, 0.1]


def test_judge_sees_top_snippets_only():
    planner = StubPlanner(plan=MULTI)
    rows = [_row("docA", i, 1.0 - i * 0.01) for i in range(20)]
    search = RecordingSearch(rows_for={"s1": rows, "s2": [], "s3": []})

    _retrieve(planner, search, settings=Settings(chat_max_context_chunks=4))

    snippets = planner.judge_calls[0]["snippets"]
    assert [s["chunk_index"] for s in snippets] == [0, 1, 2, 3]


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------

def test_planner_failure_falls_back_to_single_search():
    planner = StubPlanner(plan_raises=True)
    search = RecordingSearch()

    res = _retrieve(planner, search, query="hello")

    assert res.searches == 1
    assert res.single_intent is True
    assert [c["query"] for c in search.calls] == ["hello"]


def test_judge_failure_keeps_round_one_results():
    planner = StubPlanner(plan=MULTI, judge_raises=True)
    search = RecordingSearch()

    res = _retrieve(planner, search)

    assert res.searches == 3
    assert res.followups == []
    assert len(res.rows) == 1


def test_disabled_flag_skips_planner_entirely():
    planner = StubPlanner(plan=MULTI)
    search = RecordingSearch()

    res = _retrieve(planner, search, settings=Settings(agentic_search_enabled=False), query="hey")

    assert planner.plan_calls == []
    assert res.searches == 1
    assert [c["query"] for c in search.calls] == ["hey"]


def test_missing_planner_behaves_like_disabled():
    search = RecordingSearch()
    res = _retrieve(None, search, query="hey")
    assert res.searches == 1
    assert [c["query"] for c in search.calls] == ["hey"]


def test_one_failing_subquery_does_not_lose_the_others():
    planner = StubPlanner(plan=MULTI)
    search = RecordingSearch(
        rows_for={"s1": [_row("docA", 0, 0.5)], "s3": [_row("docC", 0, 0.6)]},
        fail_on={"s2"},
    )

    res = _retrieve(planner, search)

    assert sorted(r["doc_id"] for r in res.rows) == ["docA", "docC"]
    assert res.searches == 3


def test_all_followups_failing_keeps_round_one_results():
    planner = StubPlanner(plan=MULTI, followups=["f1", "f2"])
    search = RecordingSearch(
        rows_for={"s1": [_row("docA", 0, 0.5)], "s2": [], "s3": []},
        fail_on={"f1", "f2"},
    )

    res = _retrieve(planner, search)          # must not raise

    assert [r["doc_id"] for r in res.rows] == ["docA"]
    assert res.followups == ["f1", "f2"]
    assert res.searches == 5                  # issued, even though they failed


def test_every_subquery_failing_raises_the_underlying_error():
    planner = StubPlanner(plan=MULTI)
    search = RecordingSearch(fail_on={"s1", "s2", "s3"})

    with pytest.raises(RateLimitError):
        _retrieve(planner, search)


# --------------------------------------------------------------------------
# JSON parsing of model output
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        "[]",
        '{"single_intent": true, "subqueries": ["a", "b"]}',
        '{"single_intent": false, "subqueries": []}',
        '{"single_intent": false}',
        '{"single_intent": "yes", "subqueries": ["a"]}',
        '{"subqueries": ["a", "b"]}',              # single_intent missing → safe default
        '{"single_intent": false, "subqueries": [1, null, "  "]}',
    ],
)
def test_parse_plan_defaults_to_single_intent(text):
    assert parse_plan(text, 3).single_intent is True


def test_parse_plan_cleans_and_caps_subqueries():
    text = '{"single_intent": "false", "subqueries": ["  a  b ", "A B", "", "c", 7, "d", "e"]}'
    plan = parse_plan(text, 3)
    assert plan.single_intent is False
    assert plan.subqueries == ["a b", "c", "d"]


def test_parse_followups():
    assert parse_followups('{"sufficient": true, "followups": ["x"]}', 2) == []
    assert parse_followups('{"sufficient": false, "followups": ["x", "y", "z"]}', 2) == ["x", "y"]
    assert parse_followups('{"followups": ["x"]}', 2) == []      # sufficient missing → assume done
    assert parse_followups("garbage", 2) == []


# --------------------------------------------------------------------------
# Endpoint wiring
# --------------------------------------------------------------------------

class _StubSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _StubChatter:
    async def answer(self, query, contexts):
        return "ok", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


class _Ctx:
    def __init__(self, settings):
        self.settings = settings


@pytest.fixture
def wire(monkeypatch):
    async def _session_dep():
        yield _StubSession()

    async def _bulk_get_stub(_session, _ids):
        return []

    async def _get_dataset_stub(_session, _did):
        return None

    monkeypatch.setattr(documents_repo, "bulk_get", _bulk_get_stub)
    monkeypatch.setattr(datasets_repo, "get", _get_dataset_stub)

    def _wire(*, planner, search, settings=None, chatter=None):
        app.dependency_overrides[deps.get_session] = _session_dep
        app.dependency_overrides[deps.get_search] = lambda: search
        app.dependency_overrides[deps.get_embedder] = lambda: CountingEmbedder()
        app.dependency_overrides[deps.get_chatter] = lambda: chatter or _StubChatter()
        app.dependency_overrides[deps.get_planner] = lambda: planner
        app.dependency_overrides[deps.get_pipeline_context] = lambda: _Ctx(settings or Settings())
        return TestClient(app)

    yield _wire
    app.dependency_overrides.clear()


def test_endpoint_token_usage_sums_planner_judge_and_answer(wire):
    planner = StubPlanner(plan=MULTI, followups=["f1"], plan_usage=100, judge_usage=40)
    search = RecordingSearch()

    with wire(planner=planner, search=search) as client:
        resp = client.post("/query", json={"query": "compare A and B"})

    assert resp.status_code == 200, resp.text
    usage = resp.json()["token_usage"]
    assert usage["total_tokens"] == 100 + 40 + 15
    assert usage["prompt_tokens"] == 100 + 40 + 10
    assert len(search.calls) == 4


def test_endpoint_maps_total_search_failure_to_503(wire):
    planner = StubPlanner(plan=MULTI)
    search = RecordingSearch(fail_on={"s1", "s2", "s3"})

    with wire(planner=planner, search=search) as client:
        resp = client.post("/query", json={"query": "compare A and B"})

    assert resp.status_code == 503


def test_endpoint_empty_results_reports_planning_tokens_only(wire):
    planner = StubPlanner(plan=MULTI, plan_usage=30, judge_usage=20)
    search = RecordingSearch(default=[])

    with wire(planner=planner, search=search) as client:
        resp = client.post("/query", json={"query": "compare A and B"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "No matching documents."
    assert body["files"] == []
    assert body["token_usage"]["total_tokens"] == 50


# --------------------------------------------------------------------------
# Real QueryPlanner against a faked Azure OpenAI client
# --------------------------------------------------------------------------

class _FakeUsage:
    prompt_tokens = 7
    completion_tokens = 3
    total_tokens = 10


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = _FakeUsage()


@pytest.fixture
def real_planner():
    from app.services.query_planner import QueryPlanner
    from app.settings import get_settings

    return QueryPlanner(get_settings())


def test_real_planner_disables_sdk_retries(real_planner):
    # Bounded latency depends on tenacity being the only retry layer.
    assert real_planner._client.max_retries == 0


def test_real_planner_formats_prompts_and_uses_json_mode(real_planner):
    calls: list[dict[str, Any]] = []

    async def _fake_create(**kwargs):
        calls.append(kwargs)
        return _FakeResp('{"single_intent": false, "subqueries": ["a", "b", "c", "d"]}')

    real_planner._client.chat.completions.create = _fake_create

    plan = _run(real_planner.plan("compare A and B", max_subqueries=3))

    assert plan.single_intent is False
    assert plan.subqueries == ["a", "b", "c"]          # capped at max_subqueries
    assert plan.token_usage["total_tokens"] == 10
    kw = calls[0]
    assert kw["response_format"] == {"type": "json_object"}
    assert kw["temperature"] == 0
    assert kw["timeout"] == real_planner._timeout
    system = kw["messages"][0]["content"]
    assert '{"single_intent": true, "subqueries": []}' in system   # braces rendered, not doubled
    assert "exactly 3" in system
    assert "compare A and B" in kw["messages"][1]["content"]


def test_real_planner_judge_formats_snippets_and_caps_followups(real_planner):
    calls: list[dict[str, Any]] = []

    async def _fake_create(**kwargs):
        calls.append(kwargs)
        return _FakeResp('{"sufficient": false, "followups": ["x", "y", "z"]}')

    real_planner._client.chat.completions.create = _fake_create
    snippets = [{"doc_name": "d.pdf", "chunk_index": 2, "content": "long " * 200}]

    verdict = _run(real_planner.judge("q", ["s1", "s2"], snippets, max_followups=2))

    assert verdict.followups == ["x", "y"]
    user = calls[0]["messages"][1]["content"]
    assert "- s1\n- s2" in user
    assert "[d.pdf#2]" in user
    assert len(user) < 1500                              # snippet truncated, not the full chunk
    assert '{"sufficient": true, "followups": []}' in calls[0]["messages"][0]["content"]


def test_real_planner_retries_429_then_succeeds(real_planner):
    attempts = {"n": 0}

    async def _flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _rate_limit_error()
        return _FakeResp('{"single_intent": true, "subqueries": []}')

    real_planner._client.chat.completions.create = _flaky

    plan = _run(real_planner.plan("q", max_subqueries=3))

    assert plan.single_intent is True
    assert attempts["n"] == 2


def test_real_planner_does_not_retry_non_throttle_errors(real_planner):
    attempts = {"n": 0}

    async def _boom(**kwargs):
        attempts["n"] += 1
        raise ValueError("deployment rejected response_format")

    real_planner._client.chat.completions.create = _boom

    with pytest.raises(ValueError):
        _run(real_planner.plan("q", max_subqueries=3))
    assert attempts["n"] == 1


def test_real_planner_malformed_json_is_single_intent(real_planner):
    async def _garbage(**kwargs):
        return _FakeResp("Sure! Here are some queries: ...")

    real_planner._client.chat.completions.create = _garbage

    plan = _run(real_planner.plan("q", max_subqueries=3))
    assert plan.single_intent is True
