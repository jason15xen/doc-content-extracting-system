import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field

from app.schemas.common import PageMeta

STAGE_PROGRESS: dict[str | None, int] = {
    None: 0,
    "uploaded": 0,
    "extracted": 25,
    "chunked": 50,
    "embedded": 75,
    "indexed": 100,
    "deleted": 100,
}


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress(self) -> int:
        if self.status == "success":
            return 100
        if self.status == "failed":
            return STAGE_PROGRESS.get(self.stage, 0)
        return STAGE_PROGRESS.get(self.stage, 0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def result(self) -> str:
        if self.status == "success":
            return "completed"
        if self.status == "failed":
            stage_label = self.stage or "unknown"
            reason = self.error_message or "unknown error"
            return f"failed at stage '{stage_label}': {reason}"
        if self.status == "running":
            stage_label = self.stage or "starting"
            return f"processing ({stage_label})"
        return "queued"


class TaskListOut(BaseModel):
    items: list[TaskOut]
    meta: PageMeta
