"""sample-api spec alignment: client-supplied document IDs, per-file task progress

Revision ID: 0003_spec_alignment
Revises: 0002_task_progress_counters
Create Date: 2026-05-15

Drops and recreates ``documents`` and ``tasks`` from scratch because the
document primary key changes type (UUID → TEXT to accept client-supplied
identifiers) and the tasks table picks up a new vocabulary (action/status
values, per-file counters, timestamps). Existing rows are not migrated; the
dev DB held throwaway data and re-upload is the expected path.

The ``datasets`` table is left intact (no schema change).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_spec_alignment"
down_revision: Union[str, None] = "0002_task_progress_counters"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("idx_tasks_created_at", table_name="tasks")
    op.drop_index("idx_tasks_status", table_name="tasks")
    op.drop_index("idx_tasks_document_id", table_name="tasks")
    op.drop_table("tasks")

    op.drop_index("idx_documents_status", table_name="documents")
    op.drop_index("idx_documents_dataset_id", table_name="documents")
    op.drop_table("documents")

    op.create_table(
        "documents",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("hash", sa.CHAR(64), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "dataset_id",
            sa.Uuid(),
            sa.ForeignKey("datasets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
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
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("total_files", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_files", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "failed_file_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "skipped_file_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("current_file", sa.Text(), nullable=False, server_default=""),
        sa.Column("current_step", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('upload','delete')",
            name="ck_tasks_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed','cancelled')",
            name="ck_tasks_status",
        ),
    )
    op.create_index("idx_tasks_status", "tasks", ["status"])
    op.create_index("idx_tasks_created_at", "tasks", ["created_at"])

    op.create_table(
        "task_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("doc_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_step", sa.Text(), nullable=False, server_default="pending"),
        sa.Column(
            "action_type", sa.Text(), nullable=False, server_default="create"
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed','skipped')",
            name="ck_task_files_status",
        ),
        sa.CheckConstraint(
            "action_type IN ('create','update','delete')",
            name="ck_task_files_action_type",
        ),
    )
    op.create_index("idx_task_files_task_id", "task_files", ["task_id"])
    op.create_index("idx_task_files_status", "task_files", ["status"])


def downgrade() -> None:
    op.drop_index("idx_task_files_status", table_name="task_files")
    op.drop_index("idx_task_files_task_id", table_name="task_files")
    op.drop_table("task_files")

    op.drop_index("idx_tasks_created_at", table_name="tasks")
    op.drop_index("idx_tasks_status", table_name="tasks")
    op.drop_table("tasks")

    op.drop_index("idx_documents_status", table_name="documents")
    op.drop_index("idx_documents_dataset_id", table_name="documents")
    op.drop_table("documents")

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
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "processed_items", sa.Integer(), nullable=False, server_default="0"
        ),
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
