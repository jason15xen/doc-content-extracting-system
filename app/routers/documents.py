import json
import os
import uuid
from pathlib import Path
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DocumentStatus,
    TaskAction,
    TaskFileAction,
    TaskFileStatus,
)
from app.deps import get_pipeline_context, get_session
from app.extraction.config import SUPPORTED_EXTENSIONS
from app.pipeline.context import PipelineContext
from app.pipeline.delete import run_delete_task
from app.pipeline.ingest import run_ingest_task
from app.repositories import datasets as datasets_repo
from app.repositories import documents as documents_repo
from app.repositories import task_files as task_files_repo
from app.repositories import tasks as tasks_repo
from app.schemas.documents import (
    AsyncDeleteResponse,
    BulkDeleteRequest,
    DocumentDeleteInfo,
    DocumentInfo,
    DocumentListResponse,
)
from app.schemas.upload import (
    AsyncUploadResponse,
    FailedFileInfo,
    SkippedFileInfo,
)
from app.services.hashing import OversizeError, save_upload_with_hash
from app.services.storage import temp_upload_path, try_unlink

router = APIRouter(prefix="/doc", tags=["documents"])


def _doc_to_info(doc, dataset_name: str | None) -> DocumentInfo:
    return DocumentInfo(
        id=doc.id,
        name=doc.name,
        hash=doc.hash,
        uploaded_date=doc.uploaded_at.isoformat() if doc.uploaded_at else "",
        description=doc.description,
        file_path=doc.storage_path,
        file_size=doc.file_size,
        status=doc.status,
        dataset_id=str(doc.dataset_id) if doc.dataset_id else None,
        dataset_name=dataset_name,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
        updated_at=doc.updated_at.isoformat() if doc.updated_at else "",
    )


async def _check_no_active_task(session: AsyncSession) -> None:
    """Sample-api parity: reject a new upload/delete with 409 while another
    task is still running. Serializing batches also keeps concurrent ingests
    from piling onto the shared Azure OpenAI quota (the cause of throttled
    embeds and 40s queries)."""
    active = await tasks_repo.get_active(session)
    if active is None:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "message": (
                f"A {active.action} task is already running. "
                "Wait for it to complete or cancel it."
            ),
            "active_task": {
                "task_id": str(active.id),
                "action": active.action,
                "status": active.status,
                "total_files": active.total_files,
                "processed_files": active.processed_files,
                "failed_files": active.failed_file_count,
                "created_at": active.created_at.isoformat()
                if active.created_at
                else None,
                "status_url": f"/doc/status/{active.id}",
            },
        },
    )


def _parse_items(items_raw: str) -> list[dict]:
    """Sample-api accepts the items payload as either a bare array or an
    object with an `items` key. Mirror that."""
    try:
        parsed = json.loads(items_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    if isinstance(parsed, dict):
        items_list = parsed.get("items", parsed)
    else:
        items_list = parsed

    if not isinstance(items_list, list):
        raise HTTPException(
            status_code=400, detail="items must be a JSON array or {items:[...]}"
        )
    return items_list


@router.post(
    "",
    response_model=AsyncUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_documents(
    background: BackgroundTasks,
    files: Annotated[list[UploadFile], File(...)],
    items: Annotated[str, Form(...)],
    description: Annotated[str | None, Form()] = None,
    dataset: Annotated[str | None, Form()] = None,
    session: AsyncSession = Depends(get_session),
    ctx: PipelineContext = Depends(get_pipeline_context),
) -> AsyncUploadResponse:
    if not files:
        raise HTTPException(status_code=422, detail="no files provided")

    await _check_no_active_task(session)

    items_list = _parse_items(items)

    dataset_uuid: uuid.UUID | None = None
    dataset_name: str | None = None
    if dataset:
        try:
            dataset_uuid = uuid.UUID(dataset)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Dataset ID '{dataset}' not found. Please use a valid dataset ID from GET /datasets.",
            )
        ds_row = await datasets_repo.get(session, dataset_uuid)
        if ds_row is None:
            raise HTTPException(
                status_code=400,
                detail=f"Dataset ID '{dataset}' not found. Please use a valid dataset ID from GET /datasets.",
            )
        dataset_name = ds_row.name

    items_map: dict[str, dict] = {}
    seen_ids: set[str] = set()
    for item in items_list:
        if not all(k in item for k in ("clientFileId", "id", "fileName")):
            raise HTTPException(
                status_code=400, detail=f"Missing fields in item: {item}"
            )
        client_ext = Path(item["clientFileId"]).suffix.lower()
        target_ext = Path(item["fileName"]).suffix.lower()
        if client_ext != target_ext:
            raise HTTPException(
                status_code=400,
                detail=f"Extension mismatch: {item['clientFileId']} vs {item['fileName']}",
            )
        item_id = str(item["id"])
        if item_id in seen_ids:
            raise HTTPException(
                status_code=400, detail=f"Duplicate id: {item_id}"
            )
        seen_ids.add(item_id)
        if item["clientFileId"] in items_map:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate clientFileId: {item['clientFileId']}",
            )
        items_map[item["clientFileId"]] = item

    uploaded_names = {f.filename for f in files}
    if uploaded_names != set(items_map.keys()):
        raise HTTPException(status_code=400, detail="Files and items mismatch")

    max_bytes = ctx.settings.max_upload_mb * 1024 * 1024

    # Buckets accumulated per file then flushed into TaskFile rows + the
    # response payload.
    accepted_docs: list[dict] = []   # docs to actually process (pending)
    skipped_infos: list[SkippedFileInfo] = []
    failed_infos: list[FailedFileInfo] = []
    updated_files: list[str] = []

    for upload in files:
        client_id = upload.filename or ""
        meta = items_map[client_id]
        doc_id = str(meta["id"])
        target_name = meta["fileName"]
        ext = Path(target_name).suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            failed_infos.append(
                FailedFileInfo(
                    filename=target_name,
                    docId=doc_id,
                    reason=f"unsupported file type ({ext})",
                )
            )
            continue

        scratch_path = temp_upload_path(ext)
        try:
            hash_hex, size = await save_upload_with_hash(
                upload, scratch_path, max_bytes
            )
        except OversizeError as exc:
            try_unlink(scratch_path)
            failed_infos.append(
                FailedFileInfo(
                    filename=target_name,
                    docId=doc_id,
                    reason=f"exceeds {ctx.settings.max_upload_mb} MB limit ({exc})",
                )
            )
            continue
        except Exception as exc:
            try_unlink(scratch_path)
            failed_infos.append(
                FailedFileInfo(
                    filename=target_name,
                    docId=doc_id,
                    reason=f"save_error: {exc}",
                )
            )
            continue

        existing = await documents_repo.get(session, doc_id)
        action_type = TaskFileAction.CREATE
        is_update = False

        if existing is not None:
            if (
                existing.hash == hash_hex
                and existing.name == target_name
                and existing.status == DocumentStatus.SUCCESS.value
            ):
                try_unlink(scratch_path)
                skipped_infos.append(
                    SkippedFileInfo(
                        filename=target_name,
                        docId=doc_id,
                        reason="unchanged (same ID, filename, and content)",
                    )
                )
                continue

            is_update = True
            action_type = TaskFileAction.UPDATE
            # A re-upload that doesn't resend dataset/description keeps the
            # existing values — otherwise updating a file's content would
            # silently clear its metadata and drop it out of its dataset.
            await documents_repo.replace(
                session,
                existing,
                name=target_name,
                hash_=hash_hex,
                description=description
                if description is not None
                else existing.description,
                dataset_id=dataset_uuid if dataset else existing.dataset_id,
                storage_path=str(scratch_path),
                file_size=size,
            )
            if existing.hash == hash_hex:
                updated_files.append(f"{target_name} (filename updated, ID: {doc_id})")
            else:
                updated_files.append(f"{target_name} (content updated, ID: {doc_id})")
        else:
            try:
                await documents_repo.create(
                    session,
                    doc_id=doc_id,
                    name=target_name,
                    hash_=hash_hex,
                    description=description,
                    dataset_id=dataset_uuid,
                    storage_path=str(scratch_path),
                    file_size=size,
                )
            except IntegrityError:
                await session.rollback()
                try_unlink(scratch_path)
                failed_infos.append(
                    FailedFileInfo(
                        filename=target_name,
                        docId=doc_id,
                        reason="database insert failed (possible duplicate content under a different document ID)",
                    )
                )
                continue

        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            try_unlink(scratch_path)
            failed_infos.append(
                FailedFileInfo(
                    filename=target_name,
                    docId=doc_id,
                    reason=f"commit_error: {exc}",
                )
            )
            continue

        accepted_docs.append(
            {
                "filename": target_name,
                "doc_id": doc_id,
                "action_type": action_type.value,
                "is_update": is_update,
            }
        )

    # ----- create the task + task_files rows -----
    total_files = len(accepted_docs) + len(skipped_infos) + len(failed_infos)
    task = await tasks_repo.create(
        session,
        action=TaskAction.UPLOAD,
        description=description,
        total_files=total_files,
        failed_file_count=len(failed_infos),
        skipped_file_count=len(skipped_infos),
    )
    await session.commit()
    task_id = task.id

    rows: list[dict] = []
    for d in accepted_docs:
        rows.append(
            {
                "filename": d["filename"],
                "doc_id": d["doc_id"],
                "status": TaskFileStatus.PENDING.value,
                "action_type": d["action_type"],
            }
        )
    for s in skipped_infos:
        rows.append(
            {
                "filename": s.filename,
                "doc_id": s.docId,
                "status": TaskFileStatus.SKIPPED.value,
                "action_type": TaskFileAction.CREATE.value,
                "reason": s.reason,
            }
        )
    for f in failed_infos:
        rows.append(
            {
                "filename": f.filename,
                "doc_id": f.docId,
                "status": TaskFileStatus.FAILED.value,
                "action_type": TaskFileAction.CREATE.value,
                "reason": f.reason,
            }
        )
    await task_files_repo.create_many(session, task_id, rows)
    await session.commit()

    # If nothing made it through validation, finalize immediately as
    # completed/failed and skip the background worker.
    if not accepted_docs:
        async with ctx.sessionmaker() as fs:
            await tasks_repo.mark_completed(fs, task_id, failed=False)
            await fs.commit()
    else:
        background.add_task(
            run_ingest_task, task_id, [d["doc_id"] for d in accepted_docs]
        )

    message_parts: list[str] = []
    if accepted_docs:
        message_parts.append(f"{len(accepted_docs)} files queued")
    if skipped_infos:
        message_parts.append(f"{len(skipped_infos)} skipped")
    if failed_infos:
        message_parts.append(f"{len(failed_infos)} failed")

    return AsyncUploadResponse(
        task_id=str(task_id),
        message=", ".join(message_parts) if message_parts else "No files to process",
        status_url=f"/doc/status/{task_id}",
        total_files=total_files,
        skipped_files=skipped_infos,
        failed_files=failed_infos,
        updated_files=updated_files,
        dataset=dataset_name,
    )


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    limit: int = 100,
    offset: int = 0,
    dataset: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> DocumentListResponse:
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)

    dataset_uuid: uuid.UUID | None = None
    if dataset:
        try:
            dataset_uuid = uuid.UUID(dataset)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Dataset ID '{dataset}' not found. Please use a valid dataset ID from GET /datasets.",
            )
        ds_row = await datasets_repo.get(session, dataset_uuid)
        if ds_row is None:
            raise HTTPException(
                status_code=400,
                detail=f"Dataset ID '{dataset}' not found. Please use a valid dataset ID from GET /datasets.",
            )

    rows, total = await documents_repo.list_with_dataset_names(
        session, limit=limit, offset=offset, dataset_id=dataset_uuid
    )
    return DocumentListResponse(
        total_count=total,
        documents=[_doc_to_info(doc, ds_name) for doc, ds_name in rows],
        dataset=str(dataset_uuid) if dataset_uuid else None,
    )


@router.delete(
    "", response_model=AsyncDeleteResponse, status_code=status.HTTP_202_ACCEPTED
)
async def delete_documents(
    body: BulkDeleteRequest,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    ctx: PipelineContext = Depends(get_pipeline_context),
) -> AsyncDeleteResponse:
    if not body.doc_ids:
        raise HTTPException(status_code=400, detail="No document IDs provided")

    await _check_no_active_task(session)

    docs = await documents_repo.bulk_get(session, body.doc_ids)
    found = {d.id: d for d in docs}
    documents_info: list[DocumentDeleteInfo] = []
    not_found_ids: list[str] = []
    task_file_rows: list[dict] = []
    for doc_id in body.doc_ids:
        if doc_id in found:
            doc = found[doc_id]
            documents_info.append(DocumentDeleteInfo(docId=doc_id, name=doc.name))
            task_file_rows.append(
                {
                    "filename": doc.name,
                    "doc_id": doc_id,
                    "status": TaskFileStatus.PENDING.value,
                    "action_type": TaskFileAction.DELETE.value,
                }
            )
        else:
            not_found_ids.append(doc_id)
            task_file_rows.append(
                {
                    "filename": f"Unknown (ID: {doc_id})",
                    "doc_id": doc_id,
                    "status": TaskFileStatus.FAILED.value,
                    "action_type": TaskFileAction.DELETE.value,
                    "reason": "document not found",
                }
            )

    found_ids = [d.id for d in docs]
    task = await tasks_repo.create(
        session,
        action=TaskAction.DELETE,
        description=f"Deleting {len(body.doc_ids)} documents",
        total_files=len(body.doc_ids),
        failed_file_count=len(not_found_ids),
    )
    await session.commit()
    task_id = task.id
    await task_files_repo.create_many(session, task_id, task_file_rows)
    await session.commit()

    if found_ids:
        background.add_task(run_delete_task, task_id, found_ids)
    else:
        async with ctx.sessionmaker() as fs:
            await tasks_repo.mark_completed(fs, task_id)
            await fs.commit()

    return AsyncDeleteResponse(
        task_id=str(task_id),
        message=f"Deletion started for {len(body.doc_ids)} documents",
        status_url=f"/doc/status/{task_id}",
        total_documents=len(body.doc_ids),
        documents=documents_info,
        not_found=not_found_ids,
    )


@router.delete("/all", response_model=AsyncDeleteResponse, status_code=status.HTTP_202_ACCEPTED)
async def delete_all_documents(
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> AsyncDeleteResponse:
    await _check_no_active_task(session)

    docs = list(await documents_repo.bulk_get(session, await documents_repo.list_all_ids(session)))
    if not docs:
        raise HTTPException(status_code=404, detail="No documents found")

    doc_ids = [d.id for d in docs]
    documents_info = [DocumentDeleteInfo(docId=d.id, name=d.name) for d in docs]
    task_file_rows = [
        {
            "filename": d.name,
            "doc_id": d.id,
            "status": TaskFileStatus.PENDING.value,
            "action_type": TaskFileAction.DELETE.value,
        }
        for d in docs
    ]

    task = await tasks_repo.create(
        session,
        action=TaskAction.DELETE,
        description="DELETE ALL",
        total_files=len(doc_ids),
    )
    await session.commit()
    task_id = task.id
    await task_files_repo.create_many(session, task_id, task_file_rows)
    await session.commit()

    background.add_task(run_delete_task, task_id, doc_ids)
    return AsyncDeleteResponse(
        task_id=str(task_id),
        message=f"DELETE ALL: {len(doc_ids)} documents",
        status_url=f"/doc/status/{task_id}",
        total_documents=len(doc_ids),
        documents=documents_info,
        not_found=[],
    )
