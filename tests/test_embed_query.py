"""The query-embedding priority lane: embed_query must not queue behind
ingest batches (semaphore bypass) and must fail fast under throttling."""
import asyncio

import httpx
import pytest
from openai import RateLimitError

from app.services.embeddings import Embedder
from app.settings import get_settings


class _FakeItem:
    def __init__(self, vec):
        self.embedding = vec


class _FakeResp:
    def __init__(self, n):
        self.data = [_FakeItem([0.1] * 8) for _ in range(n)]


def _rate_limit_error() -> RateLimitError:
    req = httpx.Request("POST", "http://azure.test/embeddings")
    resp = httpx.Response(429, request=req)
    return RateLimitError("rate limited", response=resp, body=None)


@pytest.fixture()
def embedder():
    return Embedder(get_settings())


def test_embed_query_bypasses_the_batch_semaphore(embedder):
    """With every batch-semaphore slot held by (simulated) ingest batches, a
    query embed must still run immediately — it must never wait in that queue.
    If embed_query took the semaphore, this test would time out."""
    calls = []

    async def _fake_create(model, input):
        calls.append(input)
        return _FakeResp(len(input))

    embedder._client.embeddings.create = _fake_create

    async def _body():
        # Exhaust all batch slots, as a running bulk ingest would.
        slots = embedder._sem._value
        for _ in range(slots):
            await embedder._sem.acquire()
        try:
            return await asyncio.wait_for(
                embedder.embed_query("what is the reactor design?"), timeout=2.0
            )
        finally:
            for _ in range(slots):
                embedder._sem.release()

    vec = asyncio.run(_body())
    assert len(vec) == 8
    assert calls == [["what is the reactor design?"]]


def test_embed_query_fails_fast_under_throttling(embedder):
    """Sustained 429s must give up after 3 quick attempts (~2s of backoff),
    not the ingest path's 5-attempt / ~31s ladder."""
    attempts = {"n": 0}

    async def _always_429(model, input):
        attempts["n"] += 1
        raise _rate_limit_error()

    embedder._client.embeddings.create = _always_429

    async def _body():
        with pytest.raises(RateLimitError):
            await embedder.embed_query("q")

    loop = asyncio.new_event_loop()
    start = loop.time()
    try:
        loop.run_until_complete(_body())
        elapsed = loop.time() - start
    finally:
        loop.close()

    assert attempts["n"] == 3          # fail-fast attempt budget
    assert elapsed < 10                # seconds, not the ~31s batch ladder


def test_embed_query_recovers_after_transient_429(embedder):
    """One 429 then success — the retry should absorb the blip."""
    attempts = {"n": 0}

    async def _flaky(model, input):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _rate_limit_error()
        return _FakeResp(len(input))

    embedder._client.embeddings.create = _flaky
    vec = asyncio.run(embedder.embed_query("q"))
    assert len(vec) == 8
    assert attempts["n"] == 2
