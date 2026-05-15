"""Query request/response shapes — matches sample-api `POST /query`."""
from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    use_cache: bool = True
    dataset: str | None = None


class SourceFileInfo(BaseModel):
    id: str
    name: str
    relevance_score: float | None = Field(
        default=None,
        description="0-100 percentage indicating how much this source contributed.",
    )
    dataset_id: str | None = None
    dataset_name: str | None = None


class TokenUsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class QueryResponse(BaseModel):
    query: str
    answer: str
    from_cache: bool = False
    files: list[SourceFileInfo] = Field(default_factory=list)
    dataset: str | None = None
    token_usage: TokenUsageInfo | None = None
