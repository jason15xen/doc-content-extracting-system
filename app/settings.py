from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@postgres:5432/rag"
    )
    storage_dir: Path = Field(default=Path("./storage"))
    max_upload_mb: int = 100

    azure_search_endpoint: str = ""
    azure_search_api_key: str = ""
    azure_search_index_name: str = "rag-documents"

    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_embed_deployment: str = "text-embedding-3-small"
    azure_openai_chat_deployment: str = "gpt-4o-mini"

    embed_batch_size: int = 16
    chunk_tokens: int = 800
    chunk_overlap: int = 100

    search_top_k_chunks: int = 30
    search_top_k_docs: int = 5
    chat_max_context_chunks: int = 12

    ingest_concurrency: int = 2
    enable_semantic_ranking: bool = True
    ensure_index_on_startup: bool = True

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"


@lru_cache
def get_settings() -> Settings:
    return Settings()
