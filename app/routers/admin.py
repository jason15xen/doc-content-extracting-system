import uuid
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_pipeline_context, get_search, get_session
from app.pipeline.context import PipelineContext
from app.repositories import documents as documents_repo
from app.services.search_index import SearchGateway
from app.services.storage import iter_upload_files, try_unlink

router = APIRouter(prefix="/admin/cleanup", tags=["admin"])


class CleanupReport(BaseModel):
    deleted: int
    examined: int


@router.post("/orphan-files", response_model=CleanupReport)
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


@router.post("/orphan-index", response_model=CleanupReport)
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
