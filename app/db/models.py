import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKey, Index, Uuid
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


class TaskType(str, enum.Enum):
    INGEST = "ingest"
    DELETE = "delete"
    DATASET_CASCADE = "dataset_cascade"


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class PipelineStage(str, enum.Enum):
    UPLOADED = "uploaded"
    EXTRACTED = "extracted"
    CHUNKED = "chunked"
    EMBEDDED = "embedded"
    INDEXED = "indexed"
    DELETED = "deleted"


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

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(nullable=False)
    hash: Mapped[str] = mapped_column(unique=True, nullable=False)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("datasets.id", ondelete="SET NULL"),
        nullable=True,
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow
    )
    status: Mapped[str] = mapped_column(nullable=False, default=DocumentStatus.PENDING.value)
    storage_path: Mapped[str | None] = mapped_column(nullable=True)
    chunk_count: Mapped[int] = mapped_column(nullable=False, default=0)

    dataset: Mapped[Dataset | None] = relationship(back_populates="documents")
    tasks: Mapped[list["Task"]] = relationship(back_populates="document")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('ingest','delete','dataset_cascade')",
            name="ck_tasks_task_type",
        ),
        CheckConstraint(
            "status IN ('queued','running','success','failed')",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "stage IS NULL OR stage IN ('uploaded','extracted','chunked','embedded','indexed','deleted')",
            name="ck_tasks_stage",
        ),
        Index("idx_tasks_document_id", "document_id"),
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=True,
    )
    task_type: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, default=TaskStatus.QUEUED.value)
    stage: Mapped[str | None] = mapped_column(nullable=True)
    total_items: Mapped[int] = mapped_column(nullable=False, default=1)
    processed_items: Mapped[int] = mapped_column(nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    document: Mapped[Document | None] = relationship(back_populates="tasks")
