import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.common import PageMeta


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: str
    chunk_count: int
    dataset_id: uuid.UUID | None
    uploaded_at: datetime


class DocumentListOut(BaseModel):
    items: list[DocumentOut]
    meta: PageMeta


class DeleteRequest(BaseModel):
    doc_ids: list[uuid.UUID]


class DeleteAccepted(BaseModel):
    task_id: uuid.UUID
