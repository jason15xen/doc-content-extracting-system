"""LLM query planner for agentic retrieval on ``POST /query``.

Two small chat calls wrap the existing hybrid search:

  plan()  — decides whether the question is a single focused information
            need (→ one hybrid search, exactly the classic path) or needs
            several searches (comparisons, multi-part questions, several
            entities / periods / places) and, if so, writes the focused
            sub-queries.
  judge() — after the first round, looks at the retrieved snippets and
            either declares them sufficient or asks for a few follow-up
            searches targeting what is still missing.

Both are best-effort. Every failure mode (throttling, timeout, malformed
JSON, a deployment without JSON mode) surfaces to the caller as an
exception or an empty plan, and the retrieval layer degrades to a plain
hybrid search — the agentic layer can never make /query less reliable than
the classic path. Retries are fail-fast (rate limit only, three quick
attempts) because these calls sit on the interactive path.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncAzureOpenAI, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.settings import Settings

_LOG = logging.getLogger("app.query.planner")

PLAN_SYSTEM_PROMPT = (
    "You are the query planner for a document search system. Reply in JSON. "
    "Decide whether the user's question is a single focused information need "
    "that one search can answer, or whether it needs several searches: "
    "comparisons, multi-part questions, or several distinct entities, "
    "products, time periods, or places. "
    'For a single need reply {{"single_intent": true, "subqueries": []}}. '
    'Otherwise reply {{"single_intent": false, "subqueries": [...]}} with '
    "exactly {n} search queries that together cover the whole question. "
    "Each sub-query must be short (under 15 words), specific, self-contained, "
    "written in the same language as the question, and phrased the way the "
    "relevant documents would state it. Never write meta-queries such as "
    "'comparison of X and Y'; search for X and for Y separately instead. "
    "Prefer single_intent when in doubt."
)

# Wording matters here: an earlier phrasing ("written in the same language as
# the question, and different from the searches already run") made gpt-5.1
# return follow-ups translated into another language in 2 of 3 runs. Keep the
# language rule and the no-repeat rule as separate, explicit sentences.
JUDGE_SYSTEM_PROMPT = (
    "You review search results for a document question-answering system. "
    "Reply in JSON. Given the user's question, the searches already run, and "
    "the text snippets they retrieved, decide whether the snippets cover every "
    "part of the question. "
    "If every part of the question has at least one relevant snippet, reply "
    '{{"sufficient": true, "followups": []}}. '
    "Only if some part of the question has no relevant snippet at all, reply "
    '{{"sufficient": false, "followups": [...]}} with at most {n} new search '
    "queries, one per missing part, short and specific, phrased the way the "
    "documents would state it. "
    "Rules: write every follow-up in the same language as the question and "
    "never translate it; do not repeat or rephrase a search already run; "
    "never return more than {n}."
)

# Snippet length shown to the judge per chunk. Enough to tell what a chunk
# is about without paying full-chunk prompt tokens on every request.
JUDGE_SNIPPET_CHARS = 400


def zero_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def add_usage(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {k: int(a.get(k, 0) or 0) + int(b.get(k, 0) or 0) for k in zero_usage()}


def _usage_of(resp: Any) -> dict[str, int]:
    usage = getattr(resp, "usage", None)
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "total_tokens": getattr(usage, "total_tokens", 0) or 0,
    }


@dataclass
class QueryPlan:
    single_intent: bool
    subqueries: list[str] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=zero_usage)


@dataclass
class JudgeResult:
    followups: list[str] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=zero_usage)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "1"):
            return True
        if v in ("false", "no", "0"):
            return False
    return default


def _clean_queries(raw: Any, limit: int) -> list[str]:
    """Normalise a model-supplied list of queries: strings only, whitespace
    collapsed, blanks and case-insensitive duplicates dropped, hard-capped
    at ``limit`` regardless of how many the model returned."""
    if not isinstance(raw, list) or limit <= 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        q = " ".join(item.split())
        if not q:
            continue
        key = q.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= limit:
            break
    return out


def _load_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_plan(text: str, max_subqueries: int) -> QueryPlan:
    """Anything that is not a well-formed multi-intent plan is single intent —
    the safe direction, since it reproduces the classic search exactly."""
    data = _load_object(text)
    if data is None:
        return QueryPlan(single_intent=True)
    subqueries = _clean_queries(data.get("subqueries"), max_subqueries)
    if _as_bool(data.get("single_intent"), default=True) or not subqueries:
        return QueryPlan(single_intent=True)
    return QueryPlan(single_intent=False, subqueries=subqueries)


def parse_followups(text: str, max_followups: int) -> list[str]:
    data = _load_object(text)
    if data is None:
        return []
    if _as_bool(data.get("sufficient"), default=True):
        return []
    return _clean_queries(data.get("followups"), max_followups)


class QueryPlanner:
    def __init__(self, settings: Settings) -> None:
        # max_retries=0: the SDK's own retry layer (2 retries, honouring
        # Retry-After, and re-trying timeouts) would stack under the tenacity
        # policy on _complete and turn one 20s timeout into 60s. With it off
        # the worst case is three quick 429 attempts or a single timeout,
        # after which the retrieval layer falls back to the classic search.
        self._client = AsyncAzureOpenAI(
            api_key=settings.azure_openai_api_key,
            azure_endpoint=settings.azure_openai_endpoint,
            api_version=settings.azure_openai_api_version,
            max_retries=0,
        )
        self._deployment = settings.azure_openai_deployment
        self._timeout = settings.agentic_llm_timeout_s

    async def plan(self, query: str, *, max_subqueries: int) -> QueryPlan:
        user_prompt = (
            f"Question: {query}\n\n"
            f"Reply with JSON. Write exactly {max_subqueries} sub-queries "
            "only if the question needs more than one search."
        )
        text, usage = await self._complete(
            PLAN_SYSTEM_PROMPT.format(n=max_subqueries), user_prompt
        )
        plan = parse_plan(text, max_subqueries)
        plan.token_usage = usage
        return plan

    async def judge(
        self,
        query: str,
        searched: list[str],
        snippets: list[dict[str, Any]],
        *,
        max_followups: int,
    ) -> JudgeResult:
        searched_block = "\n".join(f"- {q}" for q in searched) or "- (none)"
        snippet_block = "\n\n".join(
            f"[{s.get('doc_name', '')}#{s.get('chunk_index', '')}]\n"
            f"{str(s.get('content', ''))[:JUDGE_SNIPPET_CHARS]}"
            for s in snippets
        ) or "(no results)"
        user_prompt = (
            f"Question: {query}\n\n"
            f"Searches already run:\n{searched_block}\n\n"
            f"Snippets retrieved:\n{snippet_block}\n\n"
            f"Reply with JSON. At most {max_followups} follow-up searches."
        )
        text, usage = await self._complete(
            JUDGE_SYSTEM_PROMPT.format(n=max_followups), user_prompt
        )
        return JudgeResult(
            followups=parse_followups(text, max_followups), token_usage=usage
        )

    @retry(
        retry=retry_if_exception_type(RateLimitError),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _complete(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        resp = await self._client.chat.completions.create(
            model=self._deployment,
            temperature=0,
            response_format={"type": "json_object"},
            timeout=self._timeout,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = resp.choices[0].message.content or ""
        return text, _usage_of(resp)

    async def aclose(self) -> None:
        await self._client.close()
