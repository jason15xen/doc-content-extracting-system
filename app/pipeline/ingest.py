import asyncio
import logging
import os
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import update

from app.db.models import Document, DocumentStatus, PipelineStage, Task, TaskStatus
from app.errors import EmbeddingError, PipelineError
from app.extraction.dispatcher import get_extractor
from app.pipeline.context import PipelineContext, get_context
from app.repositories import documents as documents_repo
from app.repositories import tasks as tasks_repo
from app.services.chunker import chunk_text

_LOG = logging.getLogger("app.ingest")


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
            _LOG.warning("ingest task %s vanished before start", task_id)
            return
        await tasks_repo.update_stage_status(
            session, task, stage=PipelineStage.UPLOADED, status=TaskStatus.RUNNING
        )
        await session.commit()

    started_at = time.perf_counter()
    _LOG.info(
        "ingest task %s started: %d docs (concurrency=%d)",
        task_id,
        len(doc_ids),
        ctx.settings.ingest_concurrency,
    )

    failed_ids: list[str] = []
    failed_lock = asyncio.Lock()

    async def record_failure(doc_id: uuid.UUID, exc: BaseException) -> None:
        async with failed_lock:
            failed_ids.append(f"{doc_id}:{type(exc).__name__}: {exc}")

    async def process_one(doc_id: uuid.UUID) -> None:
        # The shared ingest_semaphore governs GLOBAL concurrency across all
        # in-flight tasks (not just this request's docs), so the Azure OpenAI
        # / Azure Search / CPU load stays bounded regardless of how many
        # upload requests arrive in parallel.
        doc_start = time.perf_counter()
        try:
            async with ctx.ingest_semaphore:
                async with ctx.sessionmaker() as sess:
                    try:
                        await _ingest_one(ctx, sess, task_id, doc_id)
                        _LOG.info(
                            "doc %s indexed in %.1fms",
                            doc_id,
                            (time.perf_counter() - doc_start) * 1000.0,
                        )
                    except Exception as exc:
                        try:
                            await sess.rollback()
                        except Exception:
                            pass
                        _LOG.error(
                            "doc %s failed in %.1fms: %s",
                            doc_id,
                            (time.perf_counter() - doc_start) * 1000.0,
                            exc,
                            exc_info=True,
                        )
                        await record_failure(doc_id, exc)
        except Exception as exc:
            # Session/semaphore acquisition itself blew up — still record it.
            _LOG.error("doc %s setup failed: %s", doc_id, exc, exc_info=True)
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
            _LOG.exception(
                "progress bump failed for task %s doc %s", task_id, doc_id
            )

    try:
        # return_exceptions=True guarantees gather waits for every doc and
        # surfaces no exception — finalization below always runs.
        await asyncio.gather(
            *(process_one(d) for d in doc_ids), return_exceptions=True
        )
        elapsed_s = time.perf_counter() - started_at
        ok = len(doc_ids) - len(failed_ids)
        _LOG.info(
            "ingest task %s finished: %d/%d ok, %d failed in %.1fs",
            task_id,
            ok,
            len(doc_ids),
            len(failed_ids),
            elapsed_s,
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
            # the next boot. Log so the cause is recoverable from the file.
            _LOG.exception("ingest task %s finalize failed", task_id)


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
        # Update this task's stage/error via atomic SQL — multiple docs may
        # fail concurrently and load-modify-save would race on the in-memory
        # Task row. The aggregated error_message in the finalize block is
        # the source of truth; this per-doc write is a hint for live tailing.
        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                stage=current_stage.value,
                error_message=msg[:2000],
                updated_at=datetime.now(timezone.utc),
            )
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

    t0 = time.perf_counter()
    result = await run_in_threadpool(
        extractor.extract, document.storage_path, document.name
    )
    plain_text: str = (result.get("plain_text") or "").strip()
    if not plain_text:
        raise PipelineError("extracted", "empty extraction")
    _LOG.info(
        "doc %s extracted: %s, %d chars in %.1fms",
        document.id,
        document.name,
        len(plain_text),
        (time.perf_counter() - t0) * 1000.0,
    )
    await _set_stage(session, task_id, PipelineStage.EXTRACTED)
    await session.commit()

    t0 = time.perf_counter()
    chunks = await run_in_threadpool(
        chunk_text,
        plain_text,
        tokens=ctx.settings.chunk_tokens,
        overlap=ctx.settings.chunk_overlap,
    )
    if not chunks:
        raise PipelineError("chunked", "no chunks produced")
    _LOG.info(
        "doc %s chunked: %d chunks in %.1fms",
        document.id,
        len(chunks),
        (time.perf_counter() - t0) * 1000.0,
    )
    await _set_stage(session, task_id, PipelineStage.CHUNKED)
    await session.commit()

    t0 = time.perf_counter()
    vectors = await ctx.embedder.embed_many(chunks)
    if len(vectors) != len(chunks):
        raise EmbeddingError(
            f"embedding count mismatch: {len(vectors)} != {len(chunks)}"
        )
    _LOG.info(
        "doc %s embedded: %d vectors in %.1fms",
        document.id,
        len(vectors),
        (time.perf_counter() - t0) * 1000.0,
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
    # Capture the path now, but defer the actual unlink until AFTER commit.
    # If commit fails, the file must still exist so re-extraction is possible.
    storage_path = document.storage_path
    document.storage_path = None

    await _set_stage(session, task_id, PipelineStage.INDEXED)
    await session.commit()

    if storage_path:
        try:
            os.unlink(storage_path)
        except OSError:
            pass


async def _set_stage(session, task_id: uuid.UUID, stage: PipelineStage) -> None:
    """Update the task's stage marker via an atomic SQL UPDATE — concurrent
    stage updates across parallel docs all funnel into row-level writes
    that the DB serializes safely (last-writer-wins on the value, but no
    in-memory ORM races). The previous load-modify-save pattern would race
    on the same in-memory Task object across coroutines."""
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(stage=stage.value, updated_at=datetime.now(timezone.utc))
    )
