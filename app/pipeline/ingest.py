import asyncio
import base64
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


def _chunk_key(doc_id: str, chunk_index: int) -> str:
    """Build an Azure AI Search document key for a chunk.

    Azure restricts keys to letters, digits, '_', '-', and '='. Client-supplied
    doc_ids can contain other characters (e.g. a filename like 'tefal.pdf' — the
    '.' is rejected). URL-safe base64 maps any doc_id onto exactly the allowed
    charset and is collision-free (a naive char-replace would merge 'a.b' and
    'a-b'). The raw doc_id is still stored in the filterable `doc_id` field, so
    search and delete-by-doc_id are unaffected, and the key is never parsed back.
    """
    token = base64.urlsafe_b64encode(doc_id.encode("utf-8")).decode("ascii")
    return f"{token}_{chunk_index}"


def _build_page_note(
    scanned_pages: list[int], skipped_pages: list[int]
) -> tuple[str | None, dict | None]:
    """Build the task-file note for a successfully-indexed doc that contained
    scanned and/or unextractable pages. Three categories:
      - scanned page, text kept   -> "Scanned page"
      - scanned page, no text     -> "Scanned page (skipped — no text)"
      - non-scanned page skipped  -> "Skipped (no extractable text)" (e.g. vector
                                      outlines: no raster image, no text layer)
    `scanned_pages` are all page-filling-image pages; `skipped_pages` are all
    dropped pages, so a page can be in both (scanned with no usable text).
    Returns (reason, error_details), both None when there's nothing to note.
    """
    if not scanned_pages and not skipped_pages:
        return None, None
    skipped_set = set(skipped_pages)
    scanned_set = set(scanned_pages)
    kept_scanned = [p for p in scanned_pages if p not in skipped_set]
    skipped_scanned = [p for p in scanned_pages if p in skipped_set]
    skipped_other = [p for p in skipped_pages if p not in scanned_set]

    def _fmt(pages: list[int]) -> str:
        return ", ".join(str(p) for p in pages)

    parts: list[str] = []
    if kept_scanned:
        parts.append(f"Scanned page: {_fmt(kept_scanned)}")
    if skipped_scanned:
        parts.append(f"Scanned page (skipped — no text): {_fmt(skipped_scanned)}")
    if skipped_other:
        parts.append(f"Skipped (no extractable text): {_fmt(skipped_other)}")

    details: dict = {}
    if scanned_pages:
        details["scanned_pages"] = scanned_pages
    if skipped_pages:
        details["skipped_pages"] = skipped_pages
    return "; ".join(parts), details


async def run_ingest_task(task_id: uuid.UUID, doc_ids: Sequence[str]) -> None:
    """BackgroundTasks entrypoint. The outer try/except guarantees that even a
    catastrophic crash gets logged AND the task marked failed — an uncaught
    exception in a background task is silently swallowed by Starlette's
    runner, which would leave the task stuck at `processing` forever with no
    log trace (same guard as run_delete_task)."""
    try:
        ctx = get_context()
    except Exception:
        _LOG.exception(
            "run_ingest_task: pipeline context missing for task %s", task_id
        )
        return
    try:
        await _run(ctx, task_id, list(doc_ids))
    except Exception as exc:
        _LOG.exception("run_ingest_task crashed for task %s", task_id)
        # Best-effort: surface the crash on the task row instead of leaving it
        # pending/processing forever.
        try:
            async with ctx.sessionmaker() as session:
                await tasks_repo.mark_completed(
                    session,
                    task_id,
                    failed=True,
                    error_message=f"{type(exc).__name__}: {exc}",
                )
                await session.commit()
        except Exception:
            _LOG.exception(
                "failed to mark crashed ingest task %s as failed", task_id
            )


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

    failed_ids: set[str] = set()
    failed_lock = asyncio.Lock()

    async def record_failure(doc_id: str, exc: BaseException) -> None:
        # Per-doc error is already logged where it's caught; here we only need
        # the set of failed doc_ids for the progress bump and final count.
        async with failed_lock:
            failed_ids.add(doc_id)

    async def process_one(doc_id: str) -> None:
        doc_start = time.perf_counter()
        skipped_pages: list[int] = []
        try:
            async with ctx.ingest_semaphore:
                try:
                    skipped_pages = await _ingest_one(ctx, task_id, doc_id) or []
                    _LOG.info(
                        "doc %s indexed in %.1fms%s",
                        doc_id,
                        (time.perf_counter() - doc_start) * 1000.0,
                        f" ({len(skipped_pages)} pages skipped)" if skipped_pages else "",
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

        # Progress bump after each doc — counts processed OR failed. A doc that
        # indexed with some pages skipped still counts as processed; the skip is
        # recorded as a per-file note, not as a separate outcome. Best-effort;
        # a failed bump won't kill the pipeline.
        try:
            async with ctx.sessionmaker() as ps:
                if doc_id in failed_ids:
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
) -> list[int]:
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
        chunks, vectors, skipped_pages, scanned_pages = await _extract_chunk_embed(
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

        # Update path: now that the new chunks + vectors are built, drop the
        # old chunks just before writing the new ones. Deleting only after
        # extract/embed succeed means a mid-flight failure can never leave the
        # document with its old chunks gone and no replacement indexed.
        if action_type == TaskFileAction.UPDATE.value:
            await ctx.search.delete_by_doc_ids([doc_id])

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
            # The doc is indexed on its extractable pages, so it counts as
            # processed/completed. Attach a visible note when the document
            # contains scanned pages (text kept) and/or pages whose content
            # couldn't be extracted (skipped) — so neither is silent.
            note_reason, note_details = _build_page_note(scanned_pages, skipped_pages)
            # A file with scanned and/or unextractable pages is "partial" — it
            # indexed, but not all content was cleanly captured. A clean file
            # (no note) is "completed".
            file_status = (
                TaskFileStatus.PARTIAL.value
                if note_reason
                else TaskFileStatus.COMPLETED.value
            )
            await task_files_repo.set_progress(
                session,
                task_id,
                doc_id,
                status=file_status,
                current_step=ProcessingStep.COMPLETED.value,
                reason=note_reason,
                error_details=note_details,
            )
            await session.commit()

        return skipped_pages

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
) -> tuple[list[str], list[list[float]], list[int], list[int]]:
    if storage_path is None:
        raise PipelineError(ProcessingStep.EXTRACTING.value, "storage path missing")

    ext = os.path.splitext(doc_name)[1].lower()
    extractor = get_extractor(ext, ctx)

    t0 = time.perf_counter()
    result = await run_in_threadpool(extractor.extract, storage_path, doc_name)
    plain_text: str = (result.get("plain_text") or "").strip()
    skipped_pages: list[int] = result.get("skipped_pages") or []
    scanned_pages: list[int] = result.get("scanned_pages") or []
    if not plain_text:
        # All pages were unusable (e.g. a fully-scanned PDF with no text layer)
        # — fail the doc.
        raise PipelineError(ProcessingStep.EXTRACTING.value, "empty extraction")
    if scanned_pages or skipped_pages:
        _LOG.info(
            "doc %s: %d scanned page(s) %s, %d skipped %s",
            doc_id,
            len(scanned_pages), scanned_pages,
            len(skipped_pages), skipped_pages,
        )
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
    return chunks, vectors, skipped_pages, scanned_pages


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
            "id": _chunk_key(doc_id, i),
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
