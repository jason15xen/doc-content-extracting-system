"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-17

Dialect-neutral schema: works on SQLite (CHAR(32) UUIDs via sa.Uuid) and
PostgreSQL (native UUID). Timestamp values are owned by the application
(Python-side defaults), so no server_default here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("hash", sa.CHAR(64), nullable=False, unique=True),
        sa.Column(
            "dataset_id",
            sa.Uuid(),
            sa.ForeignKey("datasets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "status IN ('pending','processing','success','failed')",
            name="ck_documents_status",
        ),
    )
    op.create_index("idx_documents_dataset_id", "documents", ["dataset_id"])
    op.create_index("idx_documents_status", "documents", ["status"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "task_type IN ('ingest','delete','dataset_cascade')",
            name="ck_tasks_task_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','success','failed')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint(
            "stage IS NULL OR stage IN ('uploaded','extracted','chunked','embedded','indexed','deleted')",
            name="ck_tasks_stage",
        ),
    )
    op.create_index("idx_tasks_document_id", "tasks", ["document_id"])
    op.create_index("idx_tasks_status", "tasks", ["status"])
    op.create_index("idx_tasks_created_at", "tasks", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_tasks_created_at", table_name="tasks")
    op.drop_index("idx_tasks_status", table_name="tasks")
    op.drop_index("idx_tasks_document_id", table_name="tasks")
    op.drop_table("tasks")

    op.drop_index("idx_documents_status", table_name="documents")
    op.drop_index("idx_documents_dataset_id", table_name="documents")
    op.drop_table("documents")

    op.drop_table("datasets")
