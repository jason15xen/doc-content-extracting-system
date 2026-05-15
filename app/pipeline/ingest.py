import asyncio
import logging
import os
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import update

from app.db.models import (
    Document,
    DocumentStatus,
    ProcessingStep,
    TaskFile,
    TaskFileAction,
    TaskFileStatus,
)
from app.errors import EmbeddingError, PipelineError
from app.extraction.dispatcher import get_extractor
from app.pipeline.context import PipelineContext, get_context
from app.repositories import documents as documents_repo
from app.repositories import task_files as task_files_repo
from app.repositories import tasks as tasks_repo
from app.services.chunker import chunk_text

_LOG = logging.getLogger("app.ingest")


async def run_ingest_task(task_id: uuid.UUID, doc_ids: Sequence[str]) -> None:
    ctx = get_context()
    await _run(ctx, task_id, list(doc_ids))


async def _run(
    ctx: PipelineContext, task_id: uuid.UUID, doc_ids: list[str]
) -> None:
    async with ctx.sessionmaker() as session:
        task = await tasks_repo.get(session, task_id)
        if task is None:
            _LOG.warning("ingest task %s vanished before start", task_id)
            return
        await tasks_repo.mark_started(session, task_id)
        await session.commit()

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

    async def record_failure(doc_id: str, exc: BaseException) -> None:
        async with failed_lock:
            failed_ids.append(f"{doc_id}:{type(exc).__name__}: {exc}")

    async def process_one(doc_id: str) -> None:
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

        # Progress bump after each doc — counts processed_files OR
        # failed_file_count based on outcome. Best-effort; a failed bump
        # won't kill the pipeline.
        try:
            async with ctx.sessionmaker() as ps:
                if any(f.startswith(f"{doc_id}:") for f in failed_ids):
                    await tasks_repo.bump_failed(ps, task_id)
                else:
                    await tasks_repo.bump_processed(ps, task_id)
                await ps.commit()
        except Exception:
            _LOG.exception(
                "progress bump failed for task %s doc %s", task_id, doc_id
            )

    try:
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
        try:
            async with ctx.sessionmaker() as session:
                # Task is `completed` as long as the batch ran to the end —
                # per-file failures are visible in task_files. Matches sample
                # behaviour where the overall task succeeds even with some
                # failed documents.
                await tasks_repo.mark_completed(session, task_id)
                await session.commit()
        except Exception:
            _LOG.exception("ingest task %s finalize failed", task_id)


async def _ingest_one(
    ctx: PipelineContext,
    task_id: uuid.UUID,
    doc_id: str,
) -> None:
    async with ctx.sessionmaker() as session:
        document = await documents_repo.get(session, doc_id)
        if document is None:
            raise PipelineError("uploaded", "document row missing")
        task_file = await task_files_repo.get_by_task_and_doc(
            session, task_id, doc_id
        )
        action_type = task_file.action_type if task_file else TaskFileAction.CREATE.value

        document.status = DocumentStatus.PROCESSING.value
        await task_files_repo.set_progress(
            session,
            task_id,
            doc_id,
            status=TaskFileStatus.PROCESSING.value,
            current_step=ProcessingStep.EXTRACTING.value,
        )
        await tasks_repo.set_current(
            session,
            task_id,
            current_file=document.name,
            current_step=ProcessingStep.EXTRACTING.value,
        )
        await session.commit()

        doc_name = document.name
        doc_storage_path = document.storage_path
        doc_dataset_id = document.dataset_id
        doc_uploaded_at = document.uploaded_at

    current_step = ProcessingStep.EXTRACTING
    try:
        # If this is an update of an existing doc, clear out the old chunks
        # from Azure Search first so re-indexing doesn't leave stale chunks
        # behind (the new chunk count may be smaller than the old one).
        if action_type == TaskFileAction.UPDATE.value:
            await ctx.search.delete_by_doc_ids([doc_id])

        chunks, vectors = await _extract_chunk_embed(
            ctx, task_id, doc_id, doc_name, doc_storage_path
        )

        current_step = ProcessingStep.INDEXING
        async with ctx.sessionmaker() as session:
            await task_files_repo.set_progress(
                session,
                task_id,
                doc_id,
                current_step=ProcessingStep.INDEXING.value,
            )
            await session.commit()

        await _upsert_chunks(
            ctx,
            doc_id=doc_id,
            doc_name=doc_name,
            dataset_id=doc_dataset_id,
            uploaded_at=doc_uploaded_at,
            chunks=chunks,
            vectors=vectors,
        )

        async with ctx.sessionmaker() as session:
            await session.execute(
                update(Document)
                .where(Document.id == doc_id)
                .values(
                    status=DocumentStatus.SUCCESS.value,
                    storage_path=None,
                    chunk_count=len(chunks),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await task_files_repo.set_progress(
                session,
                task_id,
                doc_id,
                status=TaskFileStatus.COMPLETED.value,
                current_step=ProcessingStep.COMPLETED.value,
            )
            await session.commit()

    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        step_value = current_step.value
        if isinstance(exc, PipelineError) and exc.stage:
            step_value = exc.stage
        try:
            async with ctx.sessionmaker() as recovery:
                await recovery.execute(
                    update(Document)
                    .where(Document.id == doc_id)
                    .values(
                        status=DocumentStatus.FAILED.value,
                        storage_path=None,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await task_files_repo.set_progress(
                    recovery,
                    task_id,
                    doc_id,
                    status=TaskFileStatus.FAILED.value,
                    current_step=step_value,
                    error=msg,
                    error_details={"step": step_value, "reason": str(exc)},
                )
                await recovery.commit()
        except Exception:
            _LOG.exception(
                "recovery write failed for doc %s; deferring to reconcile",
                doc_id,
            )
        raise
    finally:
        if doc_storage_path:
            try:
                os.unlink(doc_storage_path)
            except OSError:
                pass


async def _extract_chunk_embed(
    ctx: PipelineContext,
    task_id: uuid.UUID,
    doc_id: str,
    doc_name: str,
    storage_path: str | None,
) -> tuple[list[str], list[list[float]]]:
    if storage_path is None:
        raise PipelineError(ProcessingStep.EXTRACTING.value, "storage path missing")

    ext = os.path.splitext(doc_name)[1].lower()
    extractor = get_extractor(ext, ctx)

    t0 = time.perf_counter()
    result = await run_in_threadpool(extractor.extract, storage_path, doc_name)
    plain_text: str = (result.get("plain_text") or "").strip()
    if not plain_text:
        raise PipelineError(ProcessingStep.EXTRACTING.value, "empty extraction")
    _LOG.info(
        "doc %s extracted: %s, %d chars in %.1fms",
        doc_id,
        doc_name,
        len(plain_text),
        (time.perf_counter() - t0) * 1000.0,
    )

    async with ctx.sessionmaker() as session:
        await task_files_repo.set_progress(
            session, task_id, doc_id, current_step=ProcessingStep.CHUNKING.value
        )
        await session.commit()

    t0 = time.perf_counter()
    chunks = await run_in_threadpool(
        chunk_text,
        plain_text,
        tokens=ctx.settings.chunk_tokens,
        overlap=ctx.settings.chunk_overlap,
    )
    if not chunks:
        raise PipelineError(ProcessingStep.CHUNKING.value, "no chunks produced")
    _LOG.info(
        "doc %s chunked: %d chunks in %.1fms",
        doc_id,
        len(chunks),
        (time.perf_counter() - t0) * 1000.0,
    )

    async with ctx.sessionmaker() as session:
        await task_files_repo.set_progress(
            session, task_id, doc_id, current_step=ProcessingStep.EMBEDDING.value
        )
        await session.commit()

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


async def _upsert_chunks(
    ctx: PipelineContext,
    *,
    doc_id: str,
    doc_name: str,
    dataset_id,
    uploaded_at: datetime,
    chunks: list[str],
    vectors: list[list[float]],
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


async def _sort_doc_ids_by_size(
    ctx: PipelineContext, doc_ids: list[str]
) -> list[str]:
    if len(doc_ids) <= 1:
        return doc_ids
    try:
        async with ctx.sessionmaker() as session:
            docs = await documents_repo.bulk_get(session, doc_ids)
    except Exception:
        _LOG.exception("size-sort lookup failed; preserving original order")
        return doc_ids

    _MISSING = -1
    sizes: dict[str, int] = {}
    for d in docs:
        size = _MISSING
        if d.storage_path:
            try:
                size = os.path.getsize(d.storage_path)
            except OSError:
                pass
        sizes[d.id] = size
    return sorted(
        doc_ids,
        key=lambda did: (
            sizes.get(did, _MISSING) < 0,
            -sizes.get(did, _MISSING),
        ),
    )
