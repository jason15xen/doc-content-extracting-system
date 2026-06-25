import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UTCDateTime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DocumentStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"


class TaskAction(str, enum.Enum):
    UPLOAD = "upload"
    DELETE = "delete"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskFileStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    # Indexed, but the file contained scanned and/or unextractable pages — i.e.
    # processed with caveats, not a clean completion.
    PARTIAL = "partial"


class TaskFileAction(str, enum.Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class ProcessingStep(str, enum.Enum):
    PENDING = "pending"
    EXTRACTING = "extracting_document"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    COMPLETED = "completed"
    DELETING_VECTOR = "deleting_from_vector"
    DELETING_DATABASE = "deleting_from_database"
    DELETING_STORAGE = "deleting_from_storage"


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    documents: Mapped[list["Document"]] = relationship(back_populates="dataset")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','success','failed')",
            name="ck_documents_status",
        ),
        Index("idx_documents_dataset_id", "dataset_id"),
        Index("idx_documents_status", "status"),
    )

    # Client-supplied document ID (string). Matches sample-api semantics where
    # the caller chooses the ID at upload time and uses it for skip/update
    # dedup. Stored as TEXT so it can be any reasonable identifier.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
    hash: Mapped[str] = mapped_column(unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(nullable=True)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("datasets.id", ondelete="SET NULL"),
        nullable=True,
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )
    status: Mapped[str] = mapped_column(nullable=False, default=DocumentStatus.PENDING.value)
    storage_path: Mapped[str | None] = mapped_column(nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(nullable=False, default=0)

    dataset: Mapped[Dataset | None] = relationship(back_populates="documents")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "action IN ('upload','delete')",
            name="ck_tasks_action",
        ),
        CheckConstraint(
            "status IN ('pending','processing','completed','failed','cancelled')",
            name="ck_tasks_status",
        ),
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    action: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, default=TaskStatus.PENDING.value)
    description: Mapped[str | None] = mapped_column(nullable=True)
    total_files: Mapped[int] = mapped_column(nullable=False, default=0)
    processed_files: Mapped[int] = mapped_column(nullable=False, default=0)
    failed_file_count: Mapped[int] = mapped_column(nullable=False, default=0)
    skipped_file_count: Mapped[int] = mapped_column(nullable=False, default=0)
    current_file: Mapped[str] = mapped_column(nullable=False, default="")
    current_step: Mapped[str] = mapped_column(nullable=False, default="")
    error_message: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    files: Mapped[list["TaskFile"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class TaskFile(Base):
    """Per-file row inside a task. Exposes the upload/delete progress that the
    sample-api ``/doc/status/{task_id}`` endpoint splits into ``files``,
    ``failed_files`` and ``skipped_files``. Filtering by ``status`` produces
    each of those buckets."""

    __tablename__ = "task_files"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','completed','failed','skipped','partial')",
            name="ck_task_files_status",
        ),
        CheckConstraint(
            "action_type IN ('create','update','delete')",
            name="ck_task_files_action_type",
        ),
        Index("idx_task_files_task_id", "task_id"),
        Index("idx_task_files_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(nullable=False)
    doc_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(nullable=False, default=TaskFileStatus.PENDING.value)
    current_step: Mapped[str] = mapped_column(nullable=False, default=ProcessingStep.PENDING.value)
    action_type: Mapped[str] = mapped_column(nullable=False, default=TaskFileAction.CREATE.value)
    reason: Mapped[str | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(nullable=True)
    error_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    task: Mapped[Task] = relationship(back_populates="files")
