import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.common import PageMeta


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID | None
    task_type: str
    status: str
    stage: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class TaskListOut(BaseModel):
    items: list[TaskOut]
    meta: PageMeta
