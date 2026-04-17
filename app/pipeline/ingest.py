import os
import uuid

from fastapi.concurrency import run_in_threadpool

from app.db.models import Document, DocumentStatus, PipelineStage, Task, TaskStatus
from app.errors import EmbeddingError, PipelineError
from app.extraction.dispatcher import get_extractor
from app.pipeline.context import PipelineContext, get_context
from app.repositories import documents as documents_repo
from app.repositories import tasks as tasks_repo
from app.services.chunker import chunk_text


async def run_ingest_task(task_id: uuid.UUID) -> None:
    ctx = get_context()
    async with ctx.ingest_semaphore:
        await _run(ctx, task_id)


async def _run(ctx: PipelineContext, task_id: uuid.UUID) -> None:
    async with ctx.sessionmaker() as session:
        task = await tasks_repo.get(session, task_id)
        if task is None or task.document_id is None:
            return
        document = await documents_repo.get(session, task.document_id)
        if document is None:
            return

        current_stage = PipelineStage.UPLOADED
        try:
            await tasks_repo.update_stage_status(
                session, task, stage=PipelineStage.UPLOADED, status=TaskStatus.RUNNING
            )
            document.status = DocumentStatus.PROCESSING.value
            await session.commit()

            current_stage = PipelineStage.EXTRACTED
            chunks, vectors = await _extract_chunk_embed(ctx, document, session, task, current_stage)

            current_stage = PipelineStage.INDEXED
            await _upsert_and_finalize(ctx, document, chunks, vectors, session, task)

        except Exception as exc:
            await _mark_failed(session, document, task, current_stage, exc)


async def _extract_chunk_embed(
    ctx: PipelineContext,
    document: Document,
    session,
    task: Task,
    _stage_unused: PipelineStage,
) -> tuple[list[str], list[list[float]]]:
    # EXTRACT
    ext = os.path.splitext(document.name)[1].lower()
    extractor = get_extractor(ext)
    if document.storage_path is None:
        raise PipelineError("extracted", "storage path missing")
    result = await run_in_threadpool(
        extractor.extract, document.storage_path, document.name
    )
    plain_text: str = (result.get("plain_text") or "").strip()
    if not plain_text:
        raise PipelineError("extracted", "empty extraction")
    await tasks_repo.update_stage_status(session, task, stage=PipelineStage.EXTRACTED)
    await session.commit()

    # CHUNK
    chunks = chunk_text(
        plain_text,
        tokens=ctx.settings.chunk_tokens,
        overlap=ctx.settings.chunk_overlap,
    )
    if not chunks:
        raise PipelineError("chunked", "no chunks produced")
    await tasks_repo.update_stage_status(session, task, stage=PipelineStage.CHUNKED)
    await session.commit()

    # EMBED
    vectors = await ctx.embedder.embed_many(chunks)
    if len(vectors) != len(chunks):
        raise EmbeddingError(
            f"embedding count mismatch: {len(vectors)} != {len(chunks)}"
        )
    await tasks_repo.update_stage_status(session, task, stage=PipelineStage.EMBEDDED)
    await session.commit()
    return chunks, vectors


async def _upsert_and_finalize(
    ctx: PipelineContext,
    document: Document,
    chunks: list[str],
    vectors: list[list[float]],
    session,
    task: Task,
) -> None:
    uploaded_iso = document.uploaded_at.isoformat()
    search_docs = [
        {
            "id": f"{document.id}_{i}",
            "doc_id": str(document.id),
            "doc_name": document.name,
            "dataset_id": str(document.dataset_id)
            if document.dataset_id
            else None,
            "chunk_index": i,
            "content": chunks[i],
            "content_vector": vectors[i],
            "uploaded_at": uploaded_iso,
        }
        for i in range(len(chunks))
    ]
    await ctx.search.upsert_chunks(search_docs)

    # Finalize
    document.status = DocumentStatus.SUCCESS.value
    document.chunk_count = len(chunks)
    if document.storage_path:
        try:
            os.unlink(document.storage_path)
        except OSError:
            pass
    document.storage_path = None

    await tasks_repo.update_stage_status(
        session,
        task,
        stage=PipelineStage.INDEXED,
        status=TaskStatus.SUCCESS,
    )
    task.error_message = None
    await session.commit()


async def _mark_failed(
    session,
    document: Document | None,
    task: Task,
    stage: PipelineStage,
    exc: BaseException,
) -> None:
    msg = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, PipelineError) and exc.stage:
        try:
            stage = PipelineStage(exc.stage)
        except ValueError:
            pass
    if document is not None:
        document.status = DocumentStatus.FAILED.value
    await tasks_repo.update_stage_status(
        session,
        task,
        stage=stage,
        status=TaskStatus.FAILED,
        error_message=msg,
    )
    await session.commit()
