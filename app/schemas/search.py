import uuid

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    dataset_id: uuid.UUID | None = None
    top_k: int = Field(default=5, ge=1, le=50)


class SearchSource(BaseModel):
    doc_id: uuid.UUID
    doc_name: str
    score: float


class SearchResponse(BaseModel):
    answer: str
    sources: list[SearchSource]
