from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Reads both .env (base) and .env.dev (local overrides). .env.dev wins.
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.dev"),
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Database ----
    database_url: str = Field(default="sqlite+aiosqlite:///./db/rag.db")

    # ---- Local file storage ----
    storage_dir: Path = Field(default=Path("./storage"))
    max_upload_mb: int = 100

    # ---- Azure AI Search ----
    azure_search_endpoint: str = ""
    azure_search_api_key: str = ""
    azure_search_index: str = "rag-documents"

    # ---- Azure OpenAI chat (separate resource in some setups) ----
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = ""
    azure_openai_model: str = ""
    azure_openai_api_version: str = "2024-10-21"

    # ---- Azure OpenAI embeddings (may be a different resource/endpoint) ----
    # If left blank, falls back to the chat endpoint/key above.
    azure_openai_embedding_endpoint: str = ""
    azure_openai_embedding_api_key: str = ""
    azure_openai_embedding_deployment: str = ""
    azure_openai_embedding_model: str = ""
    # Vector dimensions of the embedding model. Must match the deployed model:
    # text-embedding-3-small / ada-002 = 1536, text-embedding-3-large = 3072.
    # The search index is built with this size; a mismatch makes uploads fail.
    embedding_dimensions: int = 1536

    # ---- Pipeline tuning ----
    embed_batch_size: int = 16
    chunk_tokens: int = 800
    chunk_overlap: int = 100

    search_top_k_chunks: int = 30
    search_top_k_docs: int = 5
    chat_max_context_chunks: int = 12

    # ---- Agentic search (POST /query) ----
    # An LLM planner decides whether a question is a single focused need
    # (→ one hybrid search, exactly the classic path) or needs several
    # focused sub-queries run in parallel; a judge call may then request a
    # few follow-up searches. Hard cap on index searches per request is
    # agentic_max_searches, enforced in code regardless of model output.
    agentic_search_enabled: bool = True
    agentic_max_subqueries: int = 3
    agentic_max_searches: int = 5
    # Chunks fetched per sub-query. Lower than search_top_k_chunks so the
    # merged candidate pool (sub-queries × this) stays close to a classic
    # single search after dedup. 0 = use search_top_k_chunks.
    agentic_subquery_top_k: int = 15
    # Per-call timeout for the planner/judge LLM calls. On timeout the
    # request degrades to a plain hybrid search instead of hanging.
    agentic_llm_timeout_s: float = 20.0

    ingest_concurrency: int = 2
    # Per-instance caps on concurrent calls to upstream services. Tune
    # downward if you hit Azure OpenAI TPM limits or Azure Search 503s.
    embed_max_inflight_batches: int = 4
    search_max_inflight_uploads: int = 4

    # ---- OCR ----
    # Tesseract-based OCR for scanned PDF pages. Process pool sized to leave
    # CPU headroom for the rest of the pipeline (chunker tokenization, asyncio
    # loop, concurrent text-PDF extracts) — without that headroom OCR would
    # contend with non-scanned docs and erase the "no regression for text
    # docs" property.
    ocr_enabled: bool = True
    ocr_workers: int = 6
    ocr_dpi: int = 120
    ocr_min_chars_for_text: int = 50
    ocr_min_image_area_ratio: float = 0.9
    ocr_languages: str = "eng"
    # Classifier-only mode: when true, scan-detection still runs but ANY
    # detected scanned page rejects the whole doc instead of OCR-ing it.
    # Use this when you want to index the easy text-only docs fast and defer
    # scanned content for a separate (slower) OCR pass later. Independent
    # of `ocr_enabled` — typically set with OCR_ENABLED=false.
    ocr_reject_scanned: bool = False

    # ---- Feature flags ----
    enable_semantic_ranking: bool = True
    ensure_index_on_startup: bool = True

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def logs_dir(self) -> Path:
        return self.storage_dir / "logs"

    @property
    def effective_embedding_endpoint(self) -> str:
        return self.azure_openai_embedding_endpoint or self.azure_openai_endpoint

    @property
    def effective_embedding_api_key(self) -> str:
        return self.azure_openai_embedding_api_key or self.azure_openai_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
