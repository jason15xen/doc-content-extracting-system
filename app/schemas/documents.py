"""Document list / delete shapes — matches sample-api `GET /doc` and
`DELETE /doc`."""
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class DocumentInfo(BaseModel):
    id: str
    name: str
    hash: str
    uploaded_date: str
    description: str | None = None
    file_path: str | None = None
    file_size: int | None = None
    status: str
    dataset_id: str | None = None
    dataset_name: str | None = None
    created_at: str
    updated_at: str

    @field_validator("uploaded_date", "created_at", "updated_at", mode="before")
    @classmethod
    def _coerce_datetime(cls, v):
        if isinstance(v, datetime):
            return v.isoformat()
        return v


class DocumentListResponse(BaseModel):
    total_count: int
    documents: list[DocumentInfo] = Field(default_factory=list)
    dataset: str | None = None


class BulkDeleteRequest(BaseModel):
    doc_ids: list[str] = Field(min_length=1)


class DocumentDeleteInfo(BaseModel):
    docId: str
    name: str


class AsyncDeleteResponse(BaseModel):
    task_id: str
    message: str
    status_url: str
    total_documents: int
    documents: list[DocumentDeleteInfo] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)
