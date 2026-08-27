"""Agentic retrieval for ``POST /query``.

Turns one user question into a bounded set of hybrid searches and merges
the chunk rows they return. The post-retrieval stages in the router
(group by document, top-K docs, top-N context chunks, answer, relevance
percentages) are unchanged and see exactly the row shape a single
``SearchGateway.hybrid_search`` returns.

Search budget per request (all caps enforced here, never trusted from the
model's output):

  single intent ............ 1 search   (identical to the classic path)
  multi intent, round 1 .... up to ``agentic_max_subqueries`` in parallel
  multi intent, round 2 .... up to ``agentic_max_searches`` minus round 1,
                             only if the judge asks for follow-ups

Degradation rules: planner failure → single search; judge failure → answer
from round 1; one sub-query failing → the others are still used; every
round-1 sub-query failing → the first error propagates (the router maps
throttling to 503 exactly as for the classic path); every follow-up
failing → answer from round-1 results. Agentic search off, or no planner
wired, → single search with no LLM overhead.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.query_planner import (
    JudgeResult,
    QueryPlan,
    add_usage,
    zero_usage,
)

_LOG = logging.getLogger("app.query.retrieval")


class _Embedder(Protocol):
    async def embed_query(self, text: str) -> list[float]: ...


class _Search(Protocol):
    async def hybrid_search(
        self,
        query: str,
        query_vector: list[float],
        *,
        top_k: int,
        dataset_id: uuid.UUID | None,
    ) -> list[dict[str, Any]]: ...


class _Planner(Protocol):
    async def plan(self, query: str, *, max_subqueries: int) -> QueryPlan: ...

    async def judge(
        self,
        query: str,
        searched: list[str],
        snippets: list[dict[str, Any]],
        *,
        max_followups: int,
    ) -> JudgeResult: ...


def raw_score(row: dict[str, Any]) -> float:
    """Reranker score when semantic ranking is on, RRF/BM25 score otherwise."""
    reranker = row.get("@search.reranker_score")
    if isinstance(reranker, (int, float)):
        return float(reranker)
    score = row.get("@search.score", 0.0)
    try:
        return float(score)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class RetrievalResult:
    rows: list[dict[str, Any]]
    searches: int
    single_intent: bool
    subqueries: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=zero_usage)


async def retrieve(
    query: str,
    *,
    dataset_id: uuid.UUID | None,
    embedder: _Embedder,
    search_gw: _Search,
    planner: _Planner | None,
    settings: Any,
) -> RetrievalResult:
    top_k = int(settings.search_top_k_chunks)
    enabled = bool(getattr(settings, "agentic_search_enabled", False))
    if not enabled or planner is None:
        rows = await _one_search(query, top_k, dataset_id, embedder, search_gw)
        return RetrievalResult(rows=rows, searches=1, single_intent=True, subqueries=[query])

    max_sub = max(1, int(settings.agentic_max_subqueries))
    max_total = max(max_sub, int(settings.agentic_max_searches))
    sub_top_k = int(getattr(settings, "agentic_subquery_top_k", 0) or 0) or top_k
    usage = zero_usage()

    try:
        plan = await planner.plan(query, max_subqueries=max_sub)
        usage = add_usage(usage, plan.token_usage)
    except Exception as exc:
        _LOG.warning(
            "query planner failed (%s: %s); falling back to a single hybrid search",
            type(exc).__name__,
            exc,
        )
        plan = QueryPlan(single_intent=True)

    if plan.single_intent or not plan.subqueries:
        rows = await _one_search(query, top_k, dataset_id, embedder, search_gw)
        return RetrievalResult(
            rows=rows, searches=1, single_intent=True, subqueries=[query], token_usage=usage
        )

    # Cap enforced here even though parse_plan already truncates: a planner
    # implementation that bypasses parsing (or a future prompt tweak) must
    # not be able to exceed the budget.
    subqueries = plan.subqueries[:max_sub]
    rows_by_id: dict[Any, dict[str, Any]] = {}
    searches = 0

    batches = await _fan_out(subqueries, sub_top_k, dataset_id, embedder, search_gw)
    searches += len(subqueries)
    _merge(rows_by_id, batches)

    followups: list[str] = []
    remaining = max_total - searches
    if remaining > 0:
        snippets = _top_rows(rows_by_id, int(settings.chat_max_context_chunks))
        try:
            verdict = await planner.judge(
                query, subqueries, snippets, max_followups=remaining
            )
            usage = add_usage(usage, verdict.token_usage)
            seen = {q.casefold() for q in subqueries}
            for q in verdict.followups:
                key = q.casefold()
                if key in seen:
                    continue
                seen.add(key)
                followups.append(q)
                if len(followups) >= remaining:
                    break
        except Exception as exc:
            _LOG.warning(
                "query judge failed (%s: %s); answering from round-1 results",
                type(exc).__name__,
                exc,
            )
        if followups:
            searches += len(followups)
            try:
                batches = await _fan_out(
                    followups, sub_top_k, dataset_id, embedder, search_gw
                )
            except Exception as exc:
                # Round 1 already produced results; follow-ups are best-effort
                # and must not turn a good answer into a 503.
                _LOG.warning(
                    "all follow-up searches failed (%s: %s); answering from "
                    "round-1 results",
                    type(exc).__name__,
                    exc,
                )
                batches = []
            _merge(rows_by_id, batches)

    rows = sorted(rows_by_id.values(), key=raw_score, reverse=True)
    return RetrievalResult(
        rows=rows,
        searches=searches,
        single_intent=False,
        subqueries=subqueries,
        followups=followups,
        token_usage=usage,
    )


async def _one_search(
    query: str,
    top_k: int,
    dataset_id: uuid.UUID | None,
    embedder: _Embedder,
    search_gw: _Search,
) -> list[dict[str, Any]]:
    vec = await embedder.embed_query(query)
    return await search_gw.hybrid_search(
        query, vec, top_k=top_k, dataset_id=dataset_id
    )


async def _fan_out(
    queries: list[str],
    top_k: int,
    dataset_id: uuid.UUID | None,
    embedder: _Embedder,
    search_gw: _Search,
) -> list[list[dict[str, Any]]]:
    """Run every query concurrently (embed + search each). Partial failure is
    tolerated; total failure raises the first error so the caller can map
    throttling to the same 503 the classic path produces."""
    results = await asyncio.gather(
        *(_one_search(q, top_k, dataset_id, embedder, search_gw) for q in queries),
        return_exceptions=True,
    )
    ok: list[list[dict[str, Any]]] = []
    errors: list[BaseException] = []
    for q, r in zip(queries, results):
        if isinstance(r, BaseException):
            errors.append(r)
            _LOG.warning("sub-query %r failed: %s", q[:120], r)
        else:
            ok.append(r)
    if not ok and errors:
        raise errors[0]
    return ok


def _row_key(row: dict[str, Any]) -> Any:
    return row.get("id") or (row.get("doc_id"), row.get("chunk_index"))


def _merge(
    rows_by_id: dict[Any, dict[str, Any]], batches: list[list[dict[str, Any]]]
) -> None:
    """Dedupe by chunk id across sub-queries, keeping the best-scoring copy."""
    for batch in batches:
        for row in batch:
            key = _row_key(row)
            current = rows_by_id.get(key)
            if current is None or raw_score(row) > raw_score(current):
                rows_by_id[key] = row


def _top_rows(rows_by_id: dict[Any, dict[str, Any]], n: int) -> list[dict[str, Any]]:
    return sorted(rows_by_id.values(), key=raw_score, reverse=True)[: max(0, n)]
