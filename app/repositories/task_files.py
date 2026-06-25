import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TaskFile, TaskFileAction, TaskFileStatus


async def create_many(
    session: AsyncSession,
    task_id: uuid.UUID,
    rows: Sequence[dict],
) -> list[TaskFile]:
    """Bulk-insert one row per file in the upload/delete batch.

    Each ``rows`` entry is a dict with keys:
        filename, doc_id, status, action_type, reason (optional), current_step (optional)
    """
    out: list[TaskFile] = []
    for r in rows:
        tf = TaskFile(
            task_id=task_id,
            filename=r["filename"],
            doc_id=r.get("doc_id"),
            status=r.get("status", TaskFileStatus.PENDING.value),
            current_step=r.get("current_step", "pending"),
            action_type=r.get("action_type", TaskFileAction.CREATE.value),
            reason=r.get("reason"),
        )
        session.add(tf)
        out.append(tf)
    await session.flush()
    return out


async def list_by_task(
    session: AsyncSession, task_id: uuid.UUID
) -> Sequence[TaskFile]:
    stmt = select(TaskFile).where(TaskFile.task_id == task_id).order_by(TaskFile.id)
    return (await session.execute(stmt)).scalars().all()


async def get_by_task_and_doc(
    session: AsyncSession, task_id: uuid.UUID, doc_id: str
) -> TaskFile | None:
    stmt = select(TaskFile).where(
        TaskFile.task_id == task_id, TaskFile.doc_id == doc_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_progress(
    session: AsyncSession,
    task_id: uuid.UUID,
    doc_id: str,
    *,
    status: str | None = None,
    current_step: str | None = None,
    reason: str | None = None,
    error: str | None = None,
    error_details: dict | None = None,
) -> None:
    """Atomic per-file UPDATE. Doesn't load the row, so it's safe under
    concurrent per-doc progress updates from the ingest pipeline."""
    now = datetime.now(timezone.utc)
    values: dict = {"updated_at": now}
    if status is not None:
        values["status"] = status
    if current_step is not None:
        values["current_step"] = current_step
    if reason is not None:
        values["reason"] = reason
    if error is not None:
        values["error"] = error[:2000]
    if error_details is not None:
        values["error_details"] = error_details
    await session.execute(
        update(TaskFile)
        .where(TaskFile.task_id == task_id, TaskFile.doc_id == doc_id)
        .values(**values)
    )


async def list_pending_uploads(
    session: AsyncSession, task_id: uuid.UUID
) -> Sequence[TaskFile]:
    """Used by `cancel_task` to find files not yet started — these need
    cleanup (delete DB row, unlink file)."""
    stmt = select(TaskFile).where(
        TaskFile.task_id == task_id,
        TaskFile.status == TaskFileStatus.PENDING.value,
    )
    return (await session.execute(stmt)).scalars().all()
