import os
import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PipelineStage, TaskType
from app.deps import get_pipeline_context, get_session
from app.extraction.config import SUPPORTED_EXTENSIONS
from app.pipeline.context import PipelineContext
from app.pipeline.delete import run_delete_task
from app.pipeline.ingest import run_ingest_task
from app.repositories import datasets as datasets_repo
from app.repositories import documents as documents_repo
from app.repositories import tasks as tasks_repo
from app.schemas.common import PageMeta
from app.schemas.documents import (
    DeleteAccepted,
    DeleteRequest,
    DocumentListOut,
    DocumentOut,
)
from app.schemas.upload import UploadAcceptedItem, UploadResponse
from app.services.hashing import OversizeError, save_upload_with_hash
from app.services.storage import try_unlink, upload_path

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_documents(
    background: BackgroundTasks,
    files: Annotated[list[UploadFile], File(...)],
    dataset_id: Annotated[uuid.UUID | None, Form()] = None,
    session: AsyncSession = Depends(get_session),
    ctx: PipelineContext = Depends(get_pipeline_context),
) -> UploadResponse:
    if not files:
        raise HTTPException(status_code=422, detail="no files provided")

    if dataset_id is not None:
        ds = await datasets_repo.get(session, dataset_id)
        if ds is None:
            raise HTTPException(status_code=404, detail="dataset not found")

    items: list[UploadAcceptedItem] = []
    accepted_doc_ids: list[uuid.UUID] = []
    max_bytes = ctx.settings.max_upload_mb * 1024 * 1024

    for upload in files:
        filename = upload.filename or ""
        ext = os.path.splitext(filename)[1].lower()

        if ext not in SUPPORTED_EXTENSIONS:
            items.append(
                UploadAcceptedItem(
                    filename=filename,
                    status="failed",
                    reason=f"unsupported_extension:{ext or '(none)'}",
                )
            )
            continue

        staging_id = uuid.uuid4()
        staging_path = upload_path(ctx.settings.uploads_dir, staging_id, ext)
        try:
            hash_hex, _size = await save_upload_with_hash(upload, staging_path, max_bytes)
        except OversizeError as exc:
            items.append(
                UploadAcceptedItem(
                    filename=filename, status="failed", reason=f"oversize:{exc}"
                )
            )
            continue
        except Exception as exc:
            try_unlink(staging_path)
            items.append(
                UploadAcceptedItem(
                    filename=filename, status="failed", reason=f"save_error:{exc}"
                )
            )
            continue

        existing = await documents_repo.get_by_hash(session, hash_hex)
        if existing is not None:
            try_unlink(staging_path)
            items.append(
                UploadAcceptedItem(
                    filename=filename, status="failed", reason="duplicate"
                )
            )
            continue

        doc = await documents_repo.create(
            session,
            name=filename,
            hash_=hash_hex,
            dataset_id=dataset_id,
            storage_path=str(staging_path),
        )
        final_path = upload_path(ctx.settings.uploads_dir, doc.id, ext)
        try:
            os.rename(staging_path, final_path)
        except OSError as exc:
            await session.rollback()
            try_unlink(staging_path)
            items.append(
                UploadAcceptedItem(
                    filename=filename, status="failed", reason=f"rename_error:{exc}"
                )
            )
            continue
        doc.storage_path = str(final_path)

        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            try_unlink(final_path)
            items.append(
                UploadAcceptedItem(
                    filename=filename, status="failed", reason=f"commit_error:{exc}"
                )
            )
            continue

        accepted_doc_ids.append(doc.id)
        items.append(
            UploadAcceptedItem(
                filename=filename,
                status="accepted",
                document_id=doc.id,
            )
        )

    task_id: uuid.UUID | None = None
    if accepted_doc_ids:
        task = await tasks_repo.create(
            session,
            task_type=TaskType.INGEST,
            stage=PipelineStage.UPLOADED,
            total_items=len(accepted_doc_ids),
        )
        await session.commit()
        task_id = task.id
        background.add_task(run_ingest_task, task.id, accepted_doc_ids)

    return UploadResponse(task_id=task_id, items=items)


@router.get("", response_model=DocumentListOut)
async def list_documents(
    limit: int = 50,
    offset: int = 0,
    dataset_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> DocumentListOut:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    rows, total = await documents_repo.list_paginated(
        session,
        limit=limit,
        offset=offset,
        dataset_id=dataset_id,
        status=status_filter,
    )
    return DocumentListOut(
        items=[DocumentOut.model_validate(r) for r in rows],
        meta=PageMeta(total=total, limit=limit, offset=offset),
    )


@router.delete("", response_model=DeleteAccepted, status_code=status.HTTP_202_ACCEPTED)
async def delete_documents(
    body: DeleteRequest,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> DeleteAccepted:
    if not body.doc_ids:
        raise HTTPException(status_code=422, detail="doc_ids required")
    task = await tasks_repo.create(
        session, task_type=TaskType.DELETE, total_items=len(body.doc_ids)
    )
    await session.commit()
    background.add_task(run_delete_task, task.id, list(body.doc_ids))
    return DeleteAccepted(task_id=task.id)


@router.delete("/all", status_code=status.HTTP_202_ACCEPTED)
async def delete_all_documents(
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> DeleteAccepted:
    doc_ids = await documents_repo.list_all_ids(session)
    if not doc_ids:
        raise HTTPException(status_code=404, detail="no documents to delete")
    task = await tasks_repo.create(
        session, task_type=TaskType.DELETE, total_items=len(doc_ids)
    )
    await session.commit()
    background.add_task(run_delete_task, task.id, doc_ids)
    return DeleteAccepted(task_id=task.id)
