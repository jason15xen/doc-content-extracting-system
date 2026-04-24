import asyncio
import os
import uuid
from collections.abc import Sequence

from fastapi.concurrency import run_in_threadpool

from app.db.models import Document, DocumentStatus, PipelineStage, TaskStatus
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
    await _run(ctx, task_id, list(doc_ids))


async def _run(
    ctx: PipelineContext, task_id: uuid.UUID, doc_ids: list[uuid.UUID]
) -> None:
    # Mark the task as running (one session, one short-lived transaction).
    async with ctx.sessionmaker() as session:
        task = await tasks_repo.get(session, task_id)
        if task is None:
            return
        await tasks_repo.update_stage_status(
            session, task, stage=PipelineStage.UPLOADED, status=TaskStatus.RUNNING
        )
        await session.commit()

    failed_ids: list[str] = []
    failed_lock = asyncio.Lock()

    async def record_failure(doc_id: uuid.UUID, exc: BaseException) -> None:
        async with failed_lock:
            failed_ids.append(f"{doc_id}:{type(exc).__name__}")

    async def process_one(doc_id: uuid.UUID) -> None:
        # The shared ingest_semaphore governs GLOBAL concurrency across all
        # in-flight tasks (not just this request's docs), so the Azure OpenAI
        # / Azure Search / CPU load stays bounded regardless of how many
        # upload requests arrive in parallel.
        try:
            async with ctx.ingest_semaphore:
                # Each doc gets its own DB session — SQLAlchemy async sessions
                # are NOT safe to share across concurrent coroutines.
                async with ctx.sessionmaker() as sess:
                    try:
                        await _ingest_one(ctx, sess, task_id, doc_id)
                    except Exception as exc:
                        # Clear any partial transaction state so the context
                        # manager's close() doesn't raise PendingRollbackError.
                        try:
                            await sess.rollback()
                        except Exception:
                            pass
                        await record_failure(doc_id, exc)
        except Exception as exc:
            # Session/semaphore acquisition itself blew up — still record it.
            await record_failure(doc_id, exc)

        # Progress bump runs on its own short session after the doc pipeline
        # has released its session. Never let a failed bump kill the pipeline
        # — the worst case is a slightly stale processed_items counter, and
        # the finalize block below will still run.
        try:
            async with ctx.sessionmaker() as ps:
                pt = await tasks_repo.get(ps, task_id)
                if pt is not None:
                    await tasks_repo.bump_processed(ps, pt)
                    await ps.commit()
        except Exception:
            pass

    try:
        # return_exceptions=True guarantees gather waits for every doc and
        # surfaces no exception — finalization below always runs.
        await asyncio.gather(
            *(process_one(d) for d in doc_ids), return_exceptions=True
        )
    finally:
        # Finalize task status. Wrapped so that no matter what happened
        # above, the task never stays stuck in RUNNING.
        try:
            async with ctx.sessionmaker() as session:
                task = await tasks_repo.get(session, task_id)
                if task is not None:
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
        except Exception:
            # Last resort — swallow so the background task doesn't die with
            # an unhandled exception. The task row may remain in RUNNING in
            # this extreme case, but reconcile_running_tasks sweeps it on
            # the next boot.
            pass


async def _ingest_one(
    ctx: PipelineContext,
    session,
    task_id: uuid.UUID,
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
        chunks, vectors = await _extract_chunk_embed(ctx, document, session, task_id)

        current_stage = PipelineStage.INDEXED
        await _upsert_and_finalize(ctx, document, chunks, vectors, session, task_id)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, PipelineError) and exc.stage:
            try:
                current_stage = PipelineStage(exc.stage)
            except ValueError:
                pass
        document.status = DocumentStatus.FAILED.value
        # Update this task's stage/error via the per-doc session too.
        task = await tasks_repo.get(session, task_id)
        if task is not None:
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
    task_id: uuid.UUID,
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
    await _set_stage(session, task_id, PipelineStage.EXTRACTED)
    await session.commit()

    chunks = await run_in_threadpool(
        chunk_text,
        plain_text,
        tokens=ctx.settings.chunk_tokens,
        overlap=ctx.settings.chunk_overlap,
    )
    if not chunks:
        raise PipelineError("chunked", "no chunks produced")
    await _set_stage(session, task_id, PipelineStage.CHUNKED)
    await session.commit()

    vectors = await ctx.embedder.embed_many(chunks)
    if len(vectors) != len(chunks):
        raise EmbeddingError(
            f"embedding count mismatch: {len(vectors)} != {len(chunks)}"
        )
    await _set_stage(session, task_id, PipelineStage.EMBEDDED)
    await session.commit()
    return chunks, vectors


async def _upsert_and_finalize(
    ctx: PipelineContext,
    document: Document,
    chunks: list[str],
    vectors: list[list[float]],
    session,
    task_id: uuid.UUID,
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

    await _set_stage(session, task_id, PipelineStage.INDEXED)
    await session.commit()


async def _set_stage(session, task_id: uuid.UUID, stage: PipelineStage) -> None:
    """Update the task's stage marker using whatever session the caller owns.
    Concurrent stage updates across docs are harmless (last-writer-wins)."""
    task = await tasks_repo.get(session, task_id)
    if task is not None:
        await tasks_repo.update_stage_status(session, task, stage=stage)
