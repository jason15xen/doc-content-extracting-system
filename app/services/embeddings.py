import asyncio

from openai import APIConnectionError, AsyncAzureOpenAI, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.errors import EmbeddingError
from app.settings import Settings

_MAX_INFLIGHT_BATCHES = 4


class Embedder:
    def __init__(self, settings: Settings) -> None:
        self._client = AsyncAzureOpenAI(
            api_key=settings.effective_embedding_api_key,
            azure_endpoint=settings.effective_embedding_endpoint,
            api_version=settings.azure_openai_api_version,
        )
        self._deployment = settings.azure_openai_embedding_deployment
        self._batch_size = settings.embed_batch_size

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        batches = [
            texts[i : i + self._batch_size]
            for i in range(0, len(texts), self._batch_size)
        ]
        # Cap concurrent batches per call to avoid overwhelming the TPM quota.
        sem = asyncio.Semaphore(_MAX_INFLIGHT_BATCHES)

        async def run(batch: list[str]) -> list[list[float]]:
            async with sem:
                vectors = await self._embed_batch(batch)
            if len(vectors) != len(batch):
                raise EmbeddingError(
                    f"expected {len(batch)} embeddings, got {len(vectors)}"
                )
            return vectors

        batch_results = await asyncio.gather(*(run(b) for b in batches))
        out: list[list[float]] = []
        for vectors in batch_results:
            out.extend(vectors)
        return out

    @retry(
        retry=retry_if_exception_type((RateLimitError, APIConnectionError)),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        resp = await self._client.embeddings.create(
            model=self._deployment, input=batch
        )
        return [item.embedding for item in resp.data]

    async def aclose(self) -> None:
        await self._client.close()
