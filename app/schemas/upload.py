import uuid
from typing import Literal

from pydantic import BaseModel, computed_field


class UploadAcceptedItem(BaseModel):
    filename: str
    status: Literal["accepted", "failed"]
    reason: str | None = None
    document_id: uuid.UUID | None = None


class UploadResponse(BaseModel):
    task_id: uuid.UUID | None = None  # null when every file failed validation
    items: list[UploadAcceptedItem]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return len(self.items)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def accepted(self) -> int:
        return sum(1 for i in self.items if i.status == "accepted")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed(self) -> int:
        return sum(1 for i in self.items if i.status == "failed")
