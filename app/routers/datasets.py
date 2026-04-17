import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TaskType
from app.deps import get_session
from app.pipeline.delete import run_dataset_cascade_task
from app.repositories import datasets as datasets_repo
from app.repositories import tasks as tasks_repo
from app.schemas.datasets import (
    DatasetDeleteAccepted,
    DatasetIn,
    DatasetOut,
    DatasetPatch,
)

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post("", response_model=DatasetOut, status_code=status.HTTP_201_CREATED)
async def create_dataset(
    body: DatasetIn, session: AsyncSession = Depends(get_session)
) -> DatasetOut:
    existing = await datasets_repo.get_by_name(session, body.name)
    if existing is not None:
        raise HTTPException(status_code=409, detail="dataset name already exists")
    ds = await datasets_repo.create(
        session, name=body.name, description=body.description
    )
    await session.commit()
    return DatasetOut.model_validate(ds)


@router.get("", response_model=list[DatasetOut])
async def list_datasets(
    session: AsyncSession = Depends(get_session),
) -> list[DatasetOut]:
    rows = await datasets_repo.list_all(session)
    return [DatasetOut.model_validate(r) for r in rows]


@router.patch("/{dataset_id}", response_model=DatasetOut)
async def patch_dataset(
    dataset_id: uuid.UUID,
    body: DatasetPatch,
    session: AsyncSession = Depends(get_session),
) -> DatasetOut:
    ds = await datasets_repo.get(session, dataset_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    if body.name and body.name != ds.name:
        collision = await datasets_repo.get_by_name(session, body.name)
        if collision is not None:
            raise HTTPException(status_code=409, detail="dataset name already exists")
    await datasets_repo.update(
        session, ds, name=body.name, description=body.description
    )
    await session.commit()
    return DatasetOut.model_validate(ds)


@router.delete(
    "/{dataset_id}",
    response_model=DatasetDeleteAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_dataset(
    dataset_id: uuid.UUID,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> DatasetDeleteAccepted:
    ds = await datasets_repo.get(session, dataset_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    task = await tasks_repo.create(session, task_type=TaskType.DATASET_CASCADE)
    await session.commit()
    background.add_task(run_dataset_cascade_task, task.id, dataset_id)
    return DatasetDeleteAccepted(task_id=task.id)
