import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.pipeline.delete import run_dataset_cascade_task
from app.db.models import TaskAction, Dataset
from app.repositories import datasets as datasets_repo
from app.repositories import documents as documents_repo
from app.repositories import task_files as task_files_repo
from app.repositories import tasks as tasks_repo
from app.db.models import TaskFileAction, TaskFileStatus
from app.schemas.documents import DocumentInfo

router = APIRouter(prefix="/datasets", tags=["datasets"])


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


def _dataset_payload(ds: Dataset, document_count: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(ds.id),
        "name": ds.name,
        "description": ds.description,
        "created_at": ds.created_at.isoformat() if ds.created_at else "",
        "updated_at": ds.updated_at.isoformat() if ds.updated_at else "",
    }
    if document_count is not None:
        payload["document_count"] = document_count
    return payload


@router.get("")
async def list_datasets(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = await datasets_repo.list_with_counts(session)
    return {
        "total": len(rows),
        "datasets": [_dataset_payload(ds, cnt) for ds, cnt in rows],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_dataset(
    body: DatasetCreate, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    existing = await datasets_repo.get_by_name(session, body.name)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Dataset name already exists")
    ds = await datasets_repo.create(
        session, name=body.name, description=body.description
    )
    await session.commit()
    return {"success": True, "dataset": _dataset_payload(ds, document_count=0)}


@router.get("/{dataset_id}")
async def get_dataset(
    dataset_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    ds = await datasets_repo.get(session, dataset_id)
    if ds is None:
        raise HTTPException(
            status_code=400,
            detail=f"Dataset ID '{dataset_id}' not found. Please use a valid dataset ID from GET /datasets.",
        )
    cnt = await datasets_repo.document_count(session, dataset_id)
    return _dataset_payload(ds, document_count=cnt)


@router.put("/{dataset_id}")
async def update_dataset(
    dataset_id: uuid.UUID,
    name: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    ds = await datasets_repo.get(session, dataset_id)
    if ds is None:
        raise HTTPException(
            status_code=400,
            detail=f"Dataset ID '{dataset_id}' not found. Please use a valid dataset ID from GET /datasets.",
        )
    if name and name != ds.name:
        collision = await datasets_repo.get_by_name(session, name)
        if collision is not None:
            raise HTTPException(
                status_code=400, detail="Dataset name already exists"
            )
    await datasets_repo.update(session, ds, name=name, description=description)
    await session.commit()
    return {"success": True, "message": "Dataset updated"}


@router.delete("/{dataset_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_dataset(
    dataset_id: uuid.UUID,
    background: BackgroundTasks,
    keep_documents: bool = False,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Delete a dataset.

    - ``keep_documents=false`` (default): cascade-delete all documents (DB +
      Azure Search) and remove the dataset row.
    - ``keep_documents=true``: leave documents in place (their dataset_id is
      cleared by the FK cascade) and only remove the dataset row.
    """
    ds = await datasets_repo.get(session, dataset_id)
    if ds is None:
        raise HTTPException(
            status_code=400,
            detail=f"Dataset ID '{dataset_id}' not found. Please use a valid dataset ID from GET /datasets.",
        )

    doc_ids = await documents_repo.list_ids_by_dataset(session, dataset_id)
    doc_count = len(doc_ids)
    delete_documents = not keep_documents

    docs = await documents_repo.bulk_get(session, doc_ids) if delete_documents else []

    task = await tasks_repo.create(
        session,
        action=TaskAction.DELETE,
        description=f"Delete dataset: {ds.name} ({'delete documents' if delete_documents else 'keep documents'})",
        total_files=doc_count if delete_documents else 0,
    )
    await session.commit()
    task_id = task.id

    if delete_documents and docs:
        rows = [
            {
                "filename": d.name,
                "doc_id": d.id,
                "status": TaskFileStatus.PENDING.value,
                "action_type": TaskFileAction.DELETE.value,
            }
            for d in docs
        ]
        await task_files_repo.create_many(session, task_id, rows)
        await session.commit()

    background.add_task(
        run_dataset_cascade_task,
        task_id,
        dataset_id,
        keep_documents,
        None,
    )

    return {
        "task_id": str(task_id),
        "message": f"Dataset deletion started: {ds.name}",
        "status_url": f"/doc/status/{task_id}",
        "dataset_id": str(dataset_id),
        "dataset_name": ds.name,
        "documents_to_process": doc_count,
        "action": "delete" if delete_documents else "move_to_default",
    }


@router.get("/{dataset_id}/documents")
async def list_dataset_documents(
    dataset_id: uuid.UUID,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    ds = await datasets_repo.get(session, dataset_id)
    if ds is None:
        raise HTTPException(
            status_code=400,
            detail=f"Dataset ID '{dataset_id}' not found. Please use a valid dataset ID from GET /datasets.",
        )
    limit = max(1, min(limit, 1000))
    rows, _total = await documents_repo.list_with_dataset_names(
        session, limit=limit, offset=0, dataset_id=dataset_id
    )
    return {
        "dataset": _dataset_payload(ds),
        "total_count": len(rows),
        "documents": [
            DocumentInfo(
                id=doc.id,
                name=doc.name,
                hash=doc.hash,
                uploaded_date=doc.uploaded_at.isoformat() if doc.uploaded_at else "",
                description=doc.description,
                file_path=doc.storage_path,
                file_size=doc.file_size,
                status=doc.status,
                dataset_id=str(doc.dataset_id) if doc.dataset_id else None,
                dataset_name=ds_name,
                created_at=doc.created_at.isoformat() if doc.created_at else "",
                updated_at=doc.updated_at.isoformat() if doc.updated_at else "",
            ).model_dump()
            for doc, ds_name in rows
        ],
    }
