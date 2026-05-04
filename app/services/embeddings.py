import asyncio
import logging

from openai import APIConnectionError, AsyncAzureOpenAI, RateLimitError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.errors import EmbeddingError
from app.settings import Settings

_LOG = logging.getLogger("app.embed")


class Embedder:
    def __init__(self, settings: Settings) -> None:
        self._client = AsyncAzureOpenAI(
            api_key=settings.effective_embedding_api_key,
            azure_endpoint=settings.effective_embedding_endpoint,
            api_version=settings.azure_openai_api_version,
        )
        # Round-robin across one or two deployments of the same model on the
        # same Azure resource. Each deployment has its own TPM quota, so two
        # deployments ~double effective throughput and dodge the per-deployment
        # 429s we hit on `text-embedding-3-small` at S0 tier.
        self._deployments: list[str] = [settings.azure_openai_embedding_deployment]
        if settings.azure_openai_embedding_other_deployment:
            self._deployments.append(settings.azure_openai_embedding_other_deployment)
        self._rr_idx = 0
        self._rr_lock = asyncio.Lock()
        self._batch_size = settings.embed_batch_size
        # Process-wide cap on concurrent embed batches. A per-call semaphore
        # would let N parallel docs each spin up their own pool, multiplying
        # the in-flight count by N and defeating the TPM safeguard.
        self._sem = asyncio.Semaphore(settings.embed_max_inflight_batches)

    async def _next_deployment(self) -> str:
        async with self._rr_lock:
            d = self._deployments[self._rr_idx % len(self._deployments)]
            self._rr_idx += 1
            return d

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed `texts` in batches concurrently.

        Two-pass design preserves progress on partial failure:
          1. First pass runs all batches with `gather(return_exceptions=...)`
             semantics — successful batches' vectors are kept in `results`;
             failures are collected without aborting the rest.
          2. Failed batches are retried once. Successful batches from pass 1
             are NOT re-embedded — for a 200-batch doc where one batch hit a
             transient Azure issue, this saves ~99% of the work.

        Each batch already retries up to 5× internally (tenacity, exponential
        backoff). The doc-level second pass exists for the case where the
        per-batch retries exhausted but the issue was transient.
        """
        if not texts:
            return []

        batches = [
            texts[i : i + self._batch_size]
            for i in range(0, len(texts), self._batch_size)
        ]
        n = len(batches)
        results: list[list[list[float]] | None] = [None] * n
        progress_lock = asyncio.Lock()
        progress = {"done": 0}
        # Log every ~10% of batches, but at least 10 batches between log
        # lines so small docs don't spam. For a 1-batch doc this is a single
        # line at the end.
        progress_step = max(10, n // 10)

        async def run_batch(idx: int) -> Exception | None:
            try:
                async with self._sem:
                    vectors = await self._embed_batch(batches[idx])
                if len(vectors) != len(batches[idx]):
                    raise EmbeddingError(
                        f"expected {len(batches[idx])} embeddings, got {len(vectors)}"
                    )
                results[idx] = vectors
                async with progress_lock:
                    progress["done"] += 1
                    done = progress["done"]
                if done % progress_step == 0 or done == n:
                    _LOG.info("embed progress: %d/%d batches done", done, n)
                return None
            except Exception as exc:
                return exc

        # Pass 1: run everything, keep successes, collect failures.
        pass1 = await asyncio.gather(*(run_batch(i) for i in range(n)))
        failed = [i for i, exc in enumerate(pass1) if exc is not None]

        if failed:
            _LOG.warning(
                "embed: %d/%d batches failed first pass; retrying failed batches only",
                len(failed),
                n,
            )
            pass2 = await asyncio.gather(*(run_batch(i) for i in failed))
            still_failed = [
                (failed[k], exc) for k, exc in enumerate(pass2) if exc is not None
            ]
            if still_failed:
                first_idx, first_exc = still_failed[0]
                raise EmbeddingError(
                    f"embed: {len(still_failed)}/{n} batches failed both passes "
                    f"(first failure batch={first_idx}: "
                    f"{type(first_exc).__name__}: {first_exc})"
                )

        out: list[list[float]] = []
        for vectors in results:
            assert vectors is not None  # guarded by the raise above
            out.extend(vectors)
        return out

    @retry(
        retry=retry_if_exception_type((RateLimitError, APIConnectionError)),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(5),
        reraise=True,
        # Surface every retry as a WARNING. Without this, RateLimitError /
        # APIConnectionError retries are completely silent and indistinguishable
        # from genuinely-slow Azure responses when looking at per-batch timing.
        before_sleep=before_sleep_log(_LOG, logging.WARNING),
    )
    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        deployment = await self._next_deployment()
        resp = await self._client.embeddings.create(
            model=deployment, input=batch
        )
        return [item.embedding for item in resp.data]

    async def aclose(self) -> None:
        await self._client.close()
