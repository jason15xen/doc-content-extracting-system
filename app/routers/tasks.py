import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.repositories import tasks as tasks_repo
from app.schemas.common import PageMeta
from app.schemas.tasks import TaskListOut, TaskOut

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=TaskListOut)
async def list_tasks(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    document_id: uuid.UUID | None = None,
    task_type: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> TaskListOut:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    rows, total = await tasks_repo.list_paginated(
        session,
        limit=limit,
        offset=offset,
        status=status,
        document_id=document_id,
        task_type=task_type,
    )
    return TaskListOut(
        items=[TaskOut.model_validate(r) for r in rows],
        meta=PageMeta(total=total, limit=limit, offset=offset),
    )


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> TaskOut:
    task = await tasks_repo.get(session, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskOut.model_validate(task)
