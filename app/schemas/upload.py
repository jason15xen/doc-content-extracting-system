"""Upload request/response shapes — matches sample-api `POST /doc`."""
from pydantic import BaseModel, Field


class UploadItemMetadata(BaseModel):
    """One entry inside the `items` JSON form field for `POST /doc`."""

    clientFileId: str = Field(description="Must equal an uploaded file's filename")
    id: str = Field(description="Client-supplied document ID")
    fileName: str = Field(description="Target/display filename; extension must match clientFileId")


class SkippedFileInfo(BaseModel):
    filename: str
    docId: str
    reason: str


class FailedFileInfo(BaseModel):
    filename: str
    docId: str | None = None
    reason: str


class AsyncUploadResponse(BaseModel):
    """Response for `POST /doc`."""

    task_id: str
    message: str
    status_url: str
    total_files: int
    skipped_files: list[SkippedFileInfo] = Field(default_factory=list)
    failed_files: list[FailedFileInfo] = Field(default_factory=list)
    updated_files: list[str] = Field(default_factory=list)
    dataset: str | None = None
