import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, computed_field

from app.schemas.common import PageMeta


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID | None
    task_type: str
    status: str
    stage: str | None
    total_items: int
    processed_items: int
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def processed_time(self) -> float:
        """Processing duration in seconds. While the task is queued or running,
        measures time since creation; once finished, the final duration."""
        if self.status in {"success", "failed"}:
            end = self.updated_at
        else:
            end = datetime.now(timezone.utc)
        delta = end - self.created_at
        return round(delta.total_seconds(), 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress(self) -> int:
        if self.status == "success":
            return 100
        total = max(self.total_items, 1)
        done = max(0, min(self.processed_items, total))
        return int(done / total * 100)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def result(self) -> str:
        if self.status == "success":
            return f"completed ({self.processed_items}/{self.total_items})"
        if self.status == "failed":
            stage_label = self.stage or "unknown"
            reason = self.error_message or "unknown error"
            return (
                f"failed at stage '{stage_label}' "
                f"({self.processed_items}/{self.total_items}): {reason}"
            )
        if self.status == "running":
            stage_label = self.stage or "starting"
            return (
                f"processing {stage_label} "
                f"({self.processed_items}/{self.total_items})"
            )
        return "queued"


class TaskListOut(BaseModel):
    items: list[TaskOut]
    meta: PageMeta
