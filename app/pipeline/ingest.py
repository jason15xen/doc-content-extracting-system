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

    # Process largest docs first (LPT scheduling). The dominant tail in a
    # mixed batch is the biggest doc; starting it at t=0 alongside the rest
    # of the workers lets it run in parallel with the smaller docs instead of
    # as a serial caboose, cutting total wall-clock roughly in half on the
    # typical "many small + one giant" workload. Failures to stat (missing or
    # unreadable file) still sort last; the per-doc pipeline surfaces the
    # real error.
    doc_ids = await _sort_doc_ids_by_size(ctx, doc_ids)

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
        # upload requests arrive in parallel. _ingest_one manages its own
        # short-lived sessions internally; nothing is held here.
        doc_start = time.perf_counter()
        try:
            async with ctx.ingest_semaphore:
                try:
                    await _ingest_one(ctx, task_id, doc_id)
                    _LOG.info(
                        "doc %s indexed in %.1fms",
                        doc_id,
                        (time.perf_counter() - doc_start) * 1000.0,
                    )
                except Exception as exc:
                    _LOG.error(
                        "doc %s failed in %.1fms: %s",
                        doc_id,
                        (time.perf_counter() - doc_start) * 1000.0,
                        exc,
                        exc_info=True,
                    )
                    await record_failure(doc_id, exc)
        except Exception as exc:
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
                    # Always SUCCESS at the task level — per-doc failures are
                    # visible on Document rows in the documents list, and the
                    # "N/M ok, K failed" line is preserved in the file log
                    # above. A partial-failure batch shouldn't surface as a
                    # red "failed" task in the UI when most docs indexed fine.
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
    task_id: uuid.UUID,
    doc_id: uuid.UUID,
) -> None:
    # Phase 1: load + mark PROCESSING. Short session so we don't pin a SQLite
    # connection across the minutes-long extract/embed phase below — that
    # would surface as "database is locked" on other workers' writes when
    # busy_timeout drains.
    async with ctx.sessionmaker() as session:
        document = await documents_repo.get(session, doc_id)
        if document is None:
            raise PipelineError("uploaded", "document row missing")
        document.status = DocumentStatus.PROCESSING.value
        await session.commit()
        # Snapshot the fields the rest of the pipeline needs before the
        # session closes and the ORM instance becomes detached.
        doc_name = document.name
        doc_storage_path = document.storage_path
        doc_dataset_id = document.dataset_id
        doc_uploaded_at = document.uploaded_at

    current_stage = PipelineStage.UPLOADED
    try:
        # Phase 2: extract / chunk / embed — NO DB session held.
        current_stage = PipelineStage.EXTRACTED
        chunks, vectors = await _extract_chunk_embed(
            ctx, doc_id, doc_name, doc_storage_path
        )

        # Phase 3: Azure Search upsert + final DB write — fresh session.
        current_stage = PipelineStage.INDEXED
        async with ctx.sessionmaker() as session:
            await _upsert_and_finalize(
                ctx,
                doc_id=doc_id,
                doc_name=doc_name,
                dataset_id=doc_dataset_id,
                uploaded_at=doc_uploaded_at,
                chunks=chunks,
                vectors=vectors,
                session=session,
                task_id=task_id,
            )
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, PipelineError) and exc.stage:
            try:
                current_stage = PipelineStage(exc.stage)
            except ValueError:
                pass
        # Recovery uses a fresh session — no pending ORM state from the
        # failed run to autoflush, no stale write transaction to fight.
        # storage_path is cleared here too because the temp file is unlinked
        # in the finally block below regardless of outcome.
        try:
            async with ctx.sessionmaker() as recovery:
                await recovery.execute(
                    update(Document)
                    .where(Document.id == doc_id)
                    .values(
                        status=DocumentStatus.FAILED.value,
                        storage_path=None,
                    )
                )
                await recovery.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(
                        stage=current_stage.value,
                        error_message=msg[:2000],
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await recovery.commit()
        except Exception:
            # Best-effort. If even the fresh-session recovery fails,
            # reconcile_running_tasks at the next boot will flip the doc/task
            # to FAILED. Don't let recovery failure shadow the original exc.
            _LOG.exception(
                "recovery write failed for doc %s; deferring to reconcile_running_tasks",
                doc_id,
            )
        raise
    finally:
        # Source bytes live in an OS temp file by design — never under
        # storage/uploads. Unlink unconditionally so success and failure both
        # leave nothing behind.
        if doc_storage_path:
            try:
                os.unlink(doc_storage_path)
            except OSError:
                pass


async def _extract_chunk_embed(
    ctx: PipelineContext,
    doc_id: uuid.UUID,
    doc_name: str,
    storage_path: str | None,
) -> tuple[list[str], list[list[float]]]:
    """Extract → chunk → embed. Holds no DB session — the orchestrator
    releases the session before calling this so the minutes-long extract /
    embed work doesn't pin a SQLite connection and starve other workers'
    writes."""
    if storage_path is None:
        raise PipelineError("extracted", "storage path missing")

    ext = os.path.splitext(doc_name)[1].lower()
    extractor = get_extractor(ext, ctx)

    t0 = time.perf_counter()
    result = await run_in_threadpool(extractor.extract, storage_path, doc_name)
    plain_text: str = (result.get("plain_text") or "").strip()
    if not plain_text:
        raise PipelineError("extracted", "empty extraction")
    _LOG.info(
        "doc %s extracted: %s, %d chars in %.1fms",
        doc_id,
        doc_name,
        len(plain_text),
        (time.perf_counter() - t0) * 1000.0,
    )

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
        doc_id,
        len(chunks),
        (time.perf_counter() - t0) * 1000.0,
    )

    t0 = time.perf_counter()
    vectors = await ctx.embedder.embed_many(chunks)
    if len(vectors) != len(chunks):
        raise EmbeddingError(
            f"embedding count mismatch: {len(vectors)} != {len(chunks)}"
        )
    _LOG.info(
        "doc %s embedded: %d vectors in %.1fms",
        doc_id,
        len(vectors),
        (time.perf_counter() - t0) * 1000.0,
    )
    return chunks, vectors


async def _upsert_and_finalize(
    ctx: PipelineContext,
    *,
    doc_id: uuid.UUID,
    doc_name: str,
    dataset_id: uuid.UUID | None,
    uploaded_at: datetime,
    chunks: list[str],
    vectors: list[list[float]],
    session,
    task_id: uuid.UUID,
) -> None:
    uploaded_iso = uploaded_at.isoformat()
    search_docs = [
        {
            "id": f"{doc_id}_{i}",
            "doc_id": str(doc_id),
            "doc_name": doc_name,
            "dataset_id": str(dataset_id) if dataset_id else None,
            "chunk_index": i,
            "content": chunks[i],
            "content_vector": vectors[i],
            "uploaded_at": uploaded_iso,
        }
        for i in range(len(chunks))
    ]
    await ctx.search.upsert_chunks(search_docs)

    # Raw UPDATEs by PK — no ORM identity-map needed across sessions. Single
    # short write transaction: doc + task in one shot, then commit. The
    # source temp file is unlinked by the orchestrator's finally block.
    await session.execute(
        update(Document)
        .where(Document.id == doc_id)
        .values(
            status=DocumentStatus.SUCCESS.value,
            storage_path=None,
            chunk_count=len(chunks),
        )
    )
    await _set_stage(session, task_id, PipelineStage.INDEXED)
    await session.commit()


async def _sort_doc_ids_by_size(
    ctx: PipelineContext, doc_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    if len(doc_ids) <= 1:
        return doc_ids
    try:
        async with ctx.sessionmaker() as session:
            docs = await documents_repo.bulk_get(session, doc_ids)
    except Exception:
        _LOG.exception("size-sort lookup failed; preserving original order")
        return doc_ids

    _MISSING = -1
    sizes: dict[uuid.UUID, int] = {}
    for d in docs:
        size = _MISSING
        if d.storage_path:
            try:
                size = os.path.getsize(d.storage_path)
            except OSError:
                pass
        sizes[d.id] = size
    # Largest present files first; missing files sort to the very end so the
    # per-doc pipeline can surface the real error after the real work is done.
    return sorted(
        doc_ids,
        key=lambda did: (
            sizes.get(did, _MISSING) < 0,
            -sizes.get(did, _MISSING),
        ),
    )


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
