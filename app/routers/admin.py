import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.deps import get_pipeline_context, get_search, get_session
from app.pipeline.context import PipelineContext
from app.repositories import documents as documents_repo
from app.services.search_index import SearchGateway
from app.services.storage import iter_upload_files, try_unlink

router = APIRouter(prefix="/admin", tags=["admin"])


class CleanupReport(BaseModel):
    deleted: int
    examined: int


class LogFileInfo(BaseModel):
    name: str
    size_bytes: int
    modified_at: datetime


@router.post("/cleanup/orphan-files", response_model=CleanupReport)
async def cleanup_orphan_files(
    ctx: PipelineContext = Depends(get_pipeline_context),
    session: AsyncSession = Depends(get_session),
) -> CleanupReport:
    active_ids = {str(did) for did in await documents_repo.list_all_ids(session)}
    deleted = 0
    examined = 0
    for path in iter_upload_files(ctx.settings.uploads_dir):
        examined += 1
        stem = Path(path).stem
        try:
            stem_uuid = str(uuid.UUID(stem))
        except ValueError:
            try_unlink(path)
            deleted += 1
            continue
        if stem_uuid not in active_ids:
            try_unlink(path)
            deleted += 1
    return CleanupReport(deleted=deleted, examined=examined)


@router.post("/cleanup/orphan-index", response_model=CleanupReport)
async def cleanup_orphan_index(
    search_gw: SearchGateway = Depends(get_search),
    session: AsyncSession = Depends(get_session),
) -> CleanupReport:
    index_ids = set(await search_gw.list_distinct_doc_ids())
    db_ids = {str(did) for did in await documents_repo.list_all_ids(session)}
    orphans = [i for i in index_ids if i not in db_ids]
    if orphans:
        await search_gw.delete_by_doc_ids(orphans)
    return CleanupReport(deleted=len(orphans), examined=len(index_ids))


@router.get("/logs", response_model=list[LogFileInfo])
async def list_logs(
    ctx: PipelineContext = Depends(get_pipeline_context),
) -> list[LogFileInfo]:
    logs_dir = ctx.settings.logs_dir
    if not logs_dir.exists():
        return []
    entries: list[LogFileInfo] = []
    for path in logs_dir.iterdir():
        if not path.is_file():
            continue
        stat = path.stat()
        entries.append(
            LogFileInfo(
                name=path.name,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
        )
    entries.sort(key=lambda e: e.modified_at, reverse=True)
    return entries


def _flush_log_handlers() -> None:
    """Flush every handler currently attached to the root + uvicorn loggers
    so a snapshot copy includes all pending writes."""
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error"):
        for handler in logging.getLogger(name).handlers:
            try:
                handler.flush()
            except Exception:
                pass


@router.get("/logs/{filename}")
async def download_log(
    filename: str,
    ctx: PipelineContext = Depends(get_pipeline_context),
) -> FileResponse:
    logs_dir = ctx.settings.logs_dir.resolve()
    # Path-traversal guard: requested path must resolve inside logs_dir.
    requested = (logs_dir / filename).resolve()
    try:
        requested.relative_to(logs_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not requested.is_file():
        raise HTTPException(status_code=404, detail="log file not found")

    # Snapshot to a temp file so the ongoing logger (which keeps appending
    # to the live file during this download, including the access log line
    # for *this* very request) can't race with the streaming reader and
    # cause truncated or corrupted downloads.
    _flush_log_handlers()

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="log-snapshot-", suffix=".txt")
    os.close(tmp_fd)
    try:
        shutil.copy2(requested, tmp_path)
    except OSError as exc:
        try_unlink(tmp_path)
        raise HTTPException(
            status_code=500, detail=f"snapshot failed: {exc}"
        ) from exc

    return FileResponse(
        tmp_path,
        media_type="text/plain",
        filename=requested.name,
        # Remove the snapshot after the response has streamed to the client.
        background=BackgroundTask(try_unlink, tmp_path),
    )
