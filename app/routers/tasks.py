import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TaskFileStatus, TaskStatus
from app.deps import get_session
from app.repositories import documents as documents_repo
from app.repositories import task_files as task_files_repo
from app.repositories import tasks as tasks_repo
from app.schemas.tasks import (
    FileProgressInfo,
    TaskCancelResponse,
    TaskClearResponse,
    TaskListItem,
    TaskListResponse,
    TaskStatusResponse,
)
from app.schemas.upload import FailedFileInfo, SkippedFileInfo
from app.services.storage import try_unlink

router = APIRouter(prefix="/doc", tags=["tasks"])


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _elapsed(task) -> float:
    """elapsed_seconds: how long since the task was created. Once finished,
    locks to completion_time - created_at."""
    end = task.completed_at if task.completed_at else datetime.now(timezone.utc)
    return round((end - task.created_at).total_seconds(), 2)


def _processing(task) -> float | None:
    """processing_seconds: actual work time (started_at → completed_at).
    None until processing starts."""
    if not task.started_at:
        return None
    end = task.completed_at if task.completed_at else datetime.now(timezone.utc)
    return round((end - task.started_at).total_seconds(), 2)


@router.get("/status/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> TaskStatusResponse:
    task = await tasks_repo.get(session, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    task_files = await task_files_repo.list_by_task(session, task_id)

    failed_files: list[FailedFileInfo] = []
    skipped_files: list[SkippedFileInfo] = []
    files: list[FileProgressInfo] = []
    for tf in task_files:
        if tf.status == TaskFileStatus.FAILED.value:
            failed_files.append(
                FailedFileInfo(
                    filename=tf.filename,
                    docId=tf.doc_id,
                    reason=tf.reason or tf.error or "failed",
                )
            )
        elif tf.status == TaskFileStatus.SKIPPED.value:
            skipped_files.append(
                SkippedFileInfo(
                    filename=tf.filename,
                    docId=tf.doc_id or "",
                    reason=tf.reason or "skipped",
                )
            )
        else:
            files.append(
                FileProgressInfo(
                    filename=tf.filename,
                    docId=tf.doc_id,
                    status=tf.status,
                    current_step=tf.current_step,
                    actionType=tf.action_type,
                    reason=tf.reason,
                    error=tf.error,
                    error_details=tf.error_details,
                )
            )

    return TaskStatusResponse(
        task_id=str(task.id),
        action=task.action,
        status=task.status,
        total_files=task.total_files,
        processed_files=task.processed_files,
        failed_file_count=task.failed_file_count,
        skipped_file_count=task.skipped_file_count,
        failed_files=failed_files,
        skipped_files=skipped_files,
        current_file=task.current_file,
        current_step=task.current_step,
        description=task.description,
        created_at=_iso(task.created_at) or "",
        started_at=_iso(task.started_at),
        completed_at=_iso(task.completed_at),
        elapsed_seconds=_elapsed(task),
        processing_seconds=_processing(task),
        error=task.error_message,
        files=files,
    )


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    status_filter: str | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> TaskListResponse:
    limit = max(1, min(limit, 500))
    rows, total = await tasks_repo.list_paginated(
        session, limit=limit, status=status_filter
    )
    return TaskListResponse(
        total_tasks=total,
        tasks=[
            TaskListItem(
                task_id=str(t.id),
                action=t.action,
                status=t.status,
                total_files=t.total_files,
                processed_files=t.processed_files,
                failed_file_count=t.failed_file_count,
                created_at=_iso(t.created_at) or "",
            )
            for t in rows
        ],
    )


@router.post("/tasks/{task_id}/cancel", response_model=TaskCancelResponse)
async def cancel_task(
    task_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> TaskCancelResponse:
    task = await tasks_repo.get(session, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if task.status not in (TaskStatus.PENDING.value, TaskStatus.PROCESSING.value):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel task with status '{task.status}'",
        )

    # Snapshot pending file rows BEFORE we flip the cancel flag — in-flight
    # files (status='processing') are intentionally excluded so the
    # background pipeline can finish them safely.
    pending = await task_files_repo.list_pending_uploads(session, task_id)
    pending_doc_ids = [tf.doc_id for tf in pending if tf.doc_id]
    pending_names = [tf.filename for tf in pending]

    await tasks_repo.mark_cancelled(session, task_id)
    await session.commit()

    cleaned: list[str] = []
    if task.action == "upload" and pending_doc_ids:
        docs = await documents_repo.bulk_get(session, pending_doc_ids)
        for doc in docs:
            try_unlink(doc.storage_path)
            cleaned.append(doc.name)
        if pending_doc_ids:
            await documents_repo.delete_many(session, pending_doc_ids)
            await session.commit()
    else:
        cleaned = pending_names

    return TaskCancelResponse(
        message=f"Task {task_id} cancelled — in-flight files will complete, {len(cleaned)} pending files aborted",
        cleaned_files=cleaned,
        cleaned_count=len(cleaned),
    )


@router.delete("/tasks", response_model=TaskClearResponse)
async def clear_task_history(
    session: AsyncSession = Depends(get_session),
) -> TaskClearResponse:
    cleared = await tasks_repo.clear_history(session)
    await session.commit()
    return TaskClearResponse(
        message=f"Cleared {cleared} tasks", cleared_count=cleared
    )
