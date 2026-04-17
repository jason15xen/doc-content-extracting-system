import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentStatus, PipelineStage, Task, TaskStatus, TaskType


async def create(
    session: AsyncSession,
    *,
    task_type: TaskType,
    document_id: uuid.UUID | None = None,
    status: TaskStatus = TaskStatus.QUEUED,
    stage: PipelineStage | None = None,
) -> Task:
    task = Task(
        task_type=task_type.value,
        document_id=document_id,
        status=status.value,
        stage=stage.value if stage else None,
    )
    session.add(task)
    await session.flush()
    return task


async def get(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    return await session.get(Task, task_id)


async def list_paginated(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    status: str | None = None,
    document_id: uuid.UUID | None = None,
    task_type: str | None = None,
) -> tuple[Sequence[Task], int]:
    base = select(Task)
    count_stmt = select(func.count()).select_from(Task)
    if status is not None:
        base = base.where(Task.status == status)
        count_stmt = count_stmt.where(Task.status == status)
    if document_id is not None:
        base = base.where(Task.document_id == document_id)
        count_stmt = count_stmt.where(Task.document_id == document_id)
    if task_type is not None:
        base = base.where(Task.task_type == task_type)
        count_stmt = count_stmt.where(Task.task_type == task_type)
    base = base.order_by(Task.created_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(base)).scalars().all()
    total = (await session.execute(count_stmt)).scalar_one()
    return rows, total


async def update_stage_status(
    session: AsyncSession,
    task: Task,
    *,
    stage: PipelineStage | None = None,
    status: TaskStatus | None = None,
    error_message: str | None = None,
) -> None:
    if stage is not None:
        task.stage = stage.value
    if status is not None:
        task.status = status.value
    if error_message is not None:
        task.error_message = error_message[:2000]
    task.updated_at = datetime.now(timezone.utc)


async def reconcile_running_tasks(session: AsyncSession) -> int:
    """On process boot, mark any tasks stuck in `running` as `failed` and flip
    any documents left in `processing` back to `failed` so they aren't
    permanently stuck."""
    now = datetime.now(timezone.utc)
    stmt = (
        update(Task)
        .where(Task.status == TaskStatus.RUNNING.value)
        .values(
            status=TaskStatus.FAILED.value,
            error_message="interrupted by restart",
            updated_at=now,
        )
    )
    result = await session.execute(stmt)

    await session.execute(
        update(Document)
        .where(Document.status == DocumentStatus.PROCESSING.value)
        .values(status=DocumentStatus.FAILED.value)
    )
    return result.rowcount or 0
