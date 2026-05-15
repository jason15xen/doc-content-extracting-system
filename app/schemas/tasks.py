"""Task status / list / cancel shapes — matches sample-api
`GET /doc/status/{task_id}`, `GET /doc/tasks`, `POST /doc/tasks/{id}/cancel`,
`DELETE /doc/tasks`."""
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.query import TokenUsageInfo
from app.schemas.upload import FailedFileInfo, SkippedFileInfo


class FileProgressInfo(BaseModel):
    filename: str
    docId: str | None = None
    status: str
    current_step: str
    actionType: str
    error: str | None = None
    error_details: dict[str, Any] | None = None
    token_usage: TokenUsageInfo | None = None


class TaskStatusResponse(BaseModel):
    task_id: str
    action: str = Field(default="upload")
    status: str
    total_files: int
    processed_files: int
    failed_file_count: int
    skipped_file_count: int
    failed_files: list[FailedFileInfo] = Field(default_factory=list)
    skipped_files: list[SkippedFileInfo] = Field(default_factory=list)
    current_file: str = ""
    current_step: str = ""
    description: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    elapsed_seconds: float
    processing_seconds: float | None = None
    error: str | None = None
    files: list[FileProgressInfo] = Field(default_factory=list)


class TaskListItem(BaseModel):
    task_id: str
    action: str = Field(default="upload")
    status: str
    total_files: int
    processed_files: int
    failed_file_count: int
    created_at: str


class TaskListResponse(BaseModel):
    total_tasks: int
    tasks: list[TaskListItem] = Field(default_factory=list)


class TaskCancelResponse(BaseModel):
    message: str
    cleaned_files: list[str] = Field(default_factory=list)
    cleaned_count: int


class TaskClearResponse(BaseModel):
    message: str
    cleared_count: int
