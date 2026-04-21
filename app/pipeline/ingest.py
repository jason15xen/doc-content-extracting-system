import os
import uuid
from collections.abc import Sequence

from fastapi.concurrency import run_in_threadpool

from app.db.models import Document, DocumentStatus, PipelineStage, Task, TaskStatus
from app.errors import EmbeddingError, PipelineError
from app.extraction.dispatcher import get_extractor
from app.pipeline.context import PipelineContext, get_context
from app.repositories import documents as documents_repo
from app.repositories import tasks as tasks_repo
from app.services.chunker import chunk_text


async def run_ingest_task(
    task_id: uuid.UUID, doc_ids: Sequence[uuid.UUID]
) -> None:
    ctx = get_context()
    async with ctx.ingest_semaphore:
        await _run(ctx, task_id, list(doc_ids))


async def _run(
    ctx: PipelineContext, task_id: uuid.UUID, doc_ids: list[uuid.UUID]
) -> None:
    async with ctx.sessionmaker() as session:
        task = await tasks_repo.get(session, task_id)
        if task is None:
            return

        await tasks_repo.update_stage_status(
            session, task, stage=PipelineStage.UPLOADED, status=TaskStatus.RUNNING
        )
        await session.commit()

        failed_ids: list[str] = []
        for doc_id in doc_ids:
            try:
                await _ingest_one(ctx, session, task, doc_id)
            except Exception as exc:  # noqa: BLE001
                failed_ids.append(f"{doc_id}:{type(exc).__name__}")
            finally:
                await tasks_repo.bump_processed(session, task)
                await session.commit()

        # Finalize task
        if failed_ids:
            await tasks_repo.update_stage_status(
                session,
                task,
                stage=PipelineStage.INDEXED,
                status=TaskStatus.FAILED,
                error_message="; ".join(failed_ids)[:2000],
            )
        else:
            await tasks_repo.update_stage_status(
                session,
                task,
                stage=PipelineStage.INDEXED,
                status=TaskStatus.SUCCESS,
            )
        await session.commit()


async def _ingest_one(
    ctx: PipelineContext,
    session,
    task: Task,
    doc_id: uuid.UUID,
) -> None:
    document = await documents_repo.get(session, doc_id)
    if document is None:
        raise PipelineError("uploaded", "document row missing")

    current_stage = PipelineStage.UPLOADED
    try:
        document.status = DocumentStatus.PROCESSING.value
        await session.commit()

        current_stage = PipelineStage.EXTRACTED
        chunks, vectors = await _extract_chunk_embed(ctx, document, session, task)

        current_stage = PipelineStage.INDEXED
        await _upsert_and_finalize(ctx, document, chunks, vectors, session, task)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, PipelineError) and exc.stage:
            try:
                current_stage = PipelineStage(exc.stage)
            except ValueError:
                pass
        document.status = DocumentStatus.FAILED.value
        await tasks_repo.update_stage_status(
            session,
            task,
            stage=current_stage,
            error_message=msg,
        )
        await session.commit()
        raise


async def _extract_chunk_embed(
    ctx: PipelineContext,
    document: Document,
    session,
    task: Task,
) -> tuple[list[str], list[list[float]]]:
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

    chunks = chunk_text(
        plain_text,
        tokens=ctx.settings.chunk_tokens,
        overlap=ctx.settings.chunk_overlap,
    )
    if not chunks:
        raise PipelineError("chunked", "no chunks produced")
    await tasks_repo.update_stage_status(session, task, stage=PipelineStage.CHUNKED)
    await session.commit()

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

    document.status = DocumentStatus.SUCCESS.value
    document.chunk_count = len(chunks)
    if document.storage_path:
        try:
            os.unlink(document.storage_path)
        except OSError:
            pass
    document.storage_path = None

    await tasks_repo.update_stage_status(session, task, stage=PipelineStage.INDEXED)
    await session.commit()
