import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DatasetIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class DatasetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class DatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class DatasetDeleteAccepted(BaseModel):
    task_id: uuid.UUID
