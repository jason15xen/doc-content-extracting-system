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
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@postgres:5432/rag"
    )

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

    # ---- Pipeline tuning ----
    embed_batch_size: int = 16
    chunk_tokens: int = 800
    chunk_overlap: int = 100

    search_top_k_chunks: int = 30
    search_top_k_docs: int = 5
    chat_max_context_chunks: int = 12

    ingest_concurrency: int = 2

    # ---- Feature flags ----
    enable_semantic_ranking: bool = True
    ensure_index_on_startup: bool = True

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def effective_embedding_endpoint(self) -> str:
        return self.azure_openai_embedding_endpoint or self.azure_openai_endpoint

    @property
    def effective_embedding_api_key(self) -> str:
        return self.azure_openai_embedding_api_key or self.azure_openai_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
