import uuid
from typing import Literal

from pydantic import BaseModel


class UploadAcceptedItem(BaseModel):
    filename: str
    status: Literal["accepted", "failed"]
    reason: str | None = None
    document_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None


class UploadResponse(BaseModel):
    items: list[UploadAcceptedItem]
