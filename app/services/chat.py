from openai import APIConnectionError, AsyncAzureOpenAI, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.settings import Settings


SYSTEM_PROMPT = (
    "You answer questions using only the sources provided. "
    "Cite sources inline as [doc_name#chunk_index]. "
    "If the answer is not contained in the sources, reply exactly: "
    '"I don\'t know based on the provided sources."'
)


class Chatter:
    def __init__(self, settings: Settings) -> None:
        self._client = AsyncAzureOpenAI(
            api_key=settings.azure_openai_api_key,
            azure_endpoint=settings.azure_openai_endpoint,
            api_version=settings.azure_openai_api_version,
        )
        self._deployment = settings.azure_openai_chat_deployment

    @retry(
        retry=retry_if_exception_type((RateLimitError, APIConnectionError)),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def answer(self, query: str, contexts: list[dict]) -> str:
        source_block = "\n\n".join(
            f"[{c['doc_name']}#{c['chunk_index']}]\n{c['content']}"
            for c in contexts
        )
        user_prompt = (
            f"Question: {query}\n\nSources:\n{source_block}\n\n"
            "Answer using only the sources above."
        )
        resp = await self._client.chat.completions.create(
            model=self._deployment,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        return resp.choices[0].message.content or ""

    async def aclose(self) -> None:
        await self._client.close()
