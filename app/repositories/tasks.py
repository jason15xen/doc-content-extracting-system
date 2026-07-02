import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Document,
    DocumentStatus,
    Task,
    TaskAction,
    TaskFile,
    TaskFileStatus,
    TaskStatus,
)


async def create(
    session: AsyncSession,
    *,
    action: TaskAction,
    description: str | None = None,
    total_files: int = 0,
    failed_file_count: int = 0,
    skipped_file_count: int = 0,
) -> Task:
    task = Task(
        action=action.value,
        status=TaskStatus.PENDING.value,
        description=description,
        total_files=total_files,
        processed_files=0,
        failed_file_count=failed_file_count,
        skipped_file_count=skipped_file_count,
    )
    session.add(task)
    await session.flush()
    return task


async def get(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    return await session.get(Task, task_id)


async def mark_started(session: AsyncSession, task_id: uuid.UUID) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.PROCESSING.value,
            started_at=now,
            updated_at=now,
        )
    )


async def mark_completed(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    failed: bool = False,
    error_message: str | None = None,
) -> None:
    """Final status transition. If `failed` flips on, status becomes
    'failed', else 'completed'. Sets completed_at."""
    now = datetime.now(timezone.utc)
    status = TaskStatus.FAILED.value if failed else TaskStatus.COMPLETED.value
    values: dict = {
        "status": status,
        "completed_at": now,
        "updated_at": now,
        "current_file": "",
        "current_step": "completed",
    }
    if error_message is not None:
        values["error_message"] = error_message[:2000]
    await session.execute(update(Task).where(Task.id == task_id).values(**values))


async def mark_cancelled(session: AsyncSession, task_id: uuid.UUID) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.CANCELLED.value,
            completed_at=now,
            updated_at=now,
        )
    )


async def bump_processed(session: AsyncSession, task_id: uuid.UUID, by: int = 1) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            processed_files=Task.processed_files + by,
            updated_at=now,
        )
    )


async def bump_failed(session: AsyncSession, task_id: uuid.UUID, by: int = 1) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            failed_file_count=Task.failed_file_count + by,
            updated_at=now,
        )
    )


async def set_current(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    current_file: str | None = None,
    current_step: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    values: dict = {"updated_at": now}
    if current_file is not None:
        values["current_file"] = current_file
    if current_step is not None:
        values["current_step"] = current_step
    await session.execute(update(Task).where(Task.id == task_id).values(**values))


async def list_paginated(
    session: AsyncSession,
    *,
    limit: int,
    status: str | None = None,
    action: str | None = None,
) -> tuple[Sequence[Task], int]:
    base = select(Task)
    count_stmt = select(func.count()).select_from(Task)
    if status is not None:
        base = base.where(Task.status == status)
        count_stmt = count_stmt.where(Task.status == status)
    if action is not None:
        base = base.where(Task.action == action)
        count_stmt = count_stmt.where(Task.action == action)
    base = base.order_by(Task.created_at.desc()).limit(limit)
    rows = (await session.execute(base)).scalars().all()
    total = (await session.execute(count_stmt)).scalar_one()
    return rows, total


async def reconcile_running_tasks(session: AsyncSession) -> list[str]:
    """On boot, fail any work left mid-flight by a restart.

    Ingest/delete jobs run as in-process background tasks that don't survive a
    restart, so anything still `pending` or `processing` at startup is orphaned
    — nothing will resume it. Mark those tasks, documents, AND task_files
    `failed` with a clear reason so they're visible instead of stuck forever,
    rather than leaving un-started docs `pending` indefinitely.

    We do NOT re-queue — interrupted uploads must be re-submitted. Returns the
    orphaned documents' storage paths so the caller can delete their leftover
    upload temp files (otherwise they'd leak on disk).
    """
    now = datetime.now(timezone.utc)
    doc_unfinished = (
        DocumentStatus.PENDING.value,
        DocumentStatus.PROCESSING.value,
    )

    # Grab temp-file paths before we clear them, so the caller can unlink.
    orphan_paths = list(
        (
            await session.execute(
                select(Document.storage_path).where(
                    Document.status.in_(doc_unfinished),
                    Document.storage_path.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    tf_unfinished = (
        TaskFileStatus.PENDING.value,
        TaskFileStatus.PROCESSING.value,
    )
    # Snapshot the interrupted (pending/processing) task ids BEFORE flipping
    # them. The task_file updates below are scoped to these tasks only —
    # cancelled tasks can legitimately leave `pending` task_file rows behind
    # (cancel deletes the docs but not the rows), and those must not be
    # relabelled "server restart" nor have their counters bumped.
    interrupted_task_ids = list(
        (
            await session.execute(
                select(Task.id).where(
                    Task.status.in_(
                        (TaskStatus.PENDING.value, TaskStatus.PROCESSING.value)
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    # Count the soon-to-be-failed files per task BEFORE flipping them, so the
    # tasks' failed_file_count summary matches the failed_files list.
    per_task_counts = []
    if interrupted_task_ids:
        per_task_counts = (
            await session.execute(
                select(TaskFile.task_id, func.count())
                .where(
                    TaskFile.status.in_(tf_unfinished),
                    TaskFile.task_id.in_(interrupted_task_ids),
                )
                .group_by(TaskFile.task_id)
            )
        ).all()

    # Interrupted tasks → failed. Handling `pending` too covers the case where
    # the restart hit before the worker even started.
    if interrupted_task_ids:
        await session.execute(
            update(Task)
            .where(Task.id.in_(interrupted_task_ids))
            .values(
                status=TaskStatus.FAILED.value,
                error_message="server restart",
                completed_at=now,
                updated_at=now,
            )
        )
        await session.execute(
            update(TaskFile)
            .where(
                TaskFile.status.in_(tf_unfinished),
                TaskFile.task_id.in_(interrupted_task_ids),
            )
            .values(
                status=TaskFileStatus.FAILED.value,
                current_step="failed",
                error="server restart",
                updated_at=now,
            )
        )
    # Documents are flipped unconditionally: an orphaned pending/processing doc
    # can also exist without any live task (e.g. a crash between the upload
    # commit and task creation), and nothing will ever resume it either.
    await session.execute(
        update(Document)
        .where(Document.status.in_(doc_unfinished))
        .values(
            status=DocumentStatus.FAILED.value,
            storage_path=None,
            updated_at=now,
        )
    )
    # Back-fill each affected task's failed counter with the files just failed.
    for task_id, cnt in per_task_counts:
        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                failed_file_count=Task.failed_file_count + cnt,
                updated_at=now,
            )
        )
    return orphan_paths


async def clear_history(session: AsyncSession) -> int:
    """Sample-api `DELETE /doc/tasks` — clear only finished tasks."""
    stmt = delete(Task).where(
        Task.status.in_(
            (
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
            )
        )
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def delete_one(session: AsyncSession, task_id: uuid.UUID) -> int:
    stmt = delete(Task).where(Task.id == task_id)
    result = await session.execute(stmt)
    return result.rowcount or 0
