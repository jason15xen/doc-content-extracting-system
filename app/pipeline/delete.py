import logging
import uuid
from collections.abc import Sequence

from app.db.models import DocumentStatus, ProcessingStep, TaskFileStatus
from app.pipeline.context import PipelineContext, get_context
from app.repositories import datasets as datasets_repo
from app.repositories import documents as documents_repo
from app.repositories import task_files as task_files_repo
from app.repositories import tasks as tasks_repo
from app.services.storage import try_unlink

_LOG = logging.getLogger("app.delete")


async def run_delete_task(task_id: uuid.UUID, doc_ids: Sequence[str]) -> None:
    """BackgroundTasks entrypoint. The outer try/except guarantees that even
    a catastrophic crash inside the pipeline gets logged — otherwise an
    uncaught exception in a background task is silently swallowed by
    Starlette's BackgroundTasks runner, and the task row stays at
    ``processing`` forever, exactly the symptom we hit before."""
    _LOG.info("run_delete_task entry: task=%s, doc_ids=%s", task_id, list(doc_ids))
    try:
        ctx = get_context()
        await _delete(ctx, task_id, list(doc_ids))
    except Exception:
        _LOG.exception("run_delete_task crashed for task %s", task_id)


async def run_dataset_cascade_task(
    task_id: uuid.UUID,
    dataset_id: uuid.UUID,
    keep_documents: bool,
    move_to_dataset_id: uuid.UUID | None,
) -> None:
    """Background worker for `DELETE /datasets/{id}`.

    If ``keep_documents`` is True the contained documents are reassigned to
    ``move_to_dataset_id`` (the user's chosen 'default' dataset). Otherwise
    they're cascade-deleted from the DB and Azure Search."""
    ctx = get_context()
    async with ctx.sessionmaker() as session:
        task = await tasks_repo.get(session, task_id)
        if task is None:
            return
        await tasks_repo.mark_started(session, task_id)
        await session.commit()

        try:
            doc_ids = await documents_repo.list_ids_by_dataset(session, dataset_id)

            if keep_documents:
                if move_to_dataset_id is not None and doc_ids:
                    await documents_repo.move_to_dataset(
                        session, doc_ids, move_to_dataset_id
                    )
                await datasets_repo.delete_one(session, dataset_id)
                await session.commit()
                async with ctx.sessionmaker() as s2:
                    await tasks_repo.mark_completed(s2, task_id)
                    await s2.commit()
                return

            # Hard cascade: per-file delete with progress, then drop the
            # dataset row itself.
            await _delete_doc_ids_with_progress(ctx, task_id, doc_ids)

            async with ctx.sessionmaker() as s2:
                await datasets_repo.delete_one(s2, dataset_id)
                await tasks_repo.mark_completed(s2, task_id)
                await s2.commit()
        except Exception as exc:
            _LOG.exception(
                "dataset cascade task %s failed (dataset=%s)",
                task_id,
                dataset_id,
            )
            async with ctx.sessionmaker() as s2:
                await tasks_repo.mark_completed(
                    s2,
                    task_id,
                    failed=True,
                    error_message=f"{type(exc).__name__}: {exc}",
                )
                await s2.commit()


async def _delete(
    ctx: PipelineContext, task_id: uuid.UUID, doc_ids: list[str]
) -> None:
    async with ctx.sessionmaker() as session:
        task = await tasks_repo.get(session, task_id)
        if task is None:
            return
        await tasks_repo.mark_started(session, task_id)
        await session.commit()

    try:
        await _delete_doc_ids_with_progress(ctx, task_id, doc_ids)
        async with ctx.sessionmaker() as session:
            await tasks_repo.mark_completed(session, task_id)
            await session.commit()
    except Exception as exc:
        _LOG.exception("delete task %s failed (%d docs)", task_id, len(doc_ids))
        async with ctx.sessionmaker() as session:
            await tasks_repo.mark_completed(
                session,
                task_id,
                failed=True,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            await session.commit()


async def _delete_doc_ids_with_progress(
    ctx: PipelineContext,
    task_id: uuid.UUID,
    doc_ids: list[str],
) -> None:
    """Delete documents one-by-one, updating per-file task progress and the
    task's processed_files / failed_file_count counters.

    Order per doc: Azure AI Search chunks → local file → DB row."""
    for doc_id in doc_ids:
        async with ctx.sessionmaker() as session:
            document = await documents_repo.get(session, doc_id)
            storage_path = document.storage_path if document else None
            doc_name = document.name if document else f"Unknown (ID: {doc_id})"

            await task_files_repo.set_progress(
                session,
                task_id,
                doc_id,
                status=TaskFileStatus.PROCESSING.value,
                current_step=ProcessingStep.DELETING_VECTOR.value,
            )
            await tasks_repo.set_current(
                session,
                task_id,
                current_file=doc_name,
                current_step=ProcessingStep.DELETING_VECTOR.value,
            )
            await session.commit()

        failed = False
        err_msg: str | None = None

        if document is None:
            failed = True
            err_msg = "document not found"
            _LOG.info("delete[%s] doc=%s NOT FOUND in DB", task_id, doc_id)
        else:
            _LOG.info(
                "delete[%s] doc=%s found (status=%s, has_storage_path=%s)",
                task_id, doc_id, document.status, bool(storage_path),
            )
            # Failed/pending docs were never indexed in Azure Search and have
            # no chunks to remove; calling delete on them costs a round trip
            # and, if the index is unreachable or returns 404 on the filter
            # query, would falsely fail the delete. Skip the call for those
            # and only hit Azure when the doc actually made it to SUCCESS.
            #
            # For SUCCESS docs, the Azure delete is BEST-EFFORT — a remote
            # failure must NOT block the SQLite row delete, otherwise the
            # user can never get a failed doc out of the list (the next
            # retry repeats the same Azure failure). Orphaned chunks left
            # behind are cleanable via /admin/cleanup/orphan-index.
            azure_warning: str | None = None
            if document.status == DocumentStatus.SUCCESS.value:
                _LOG.info("delete[%s] doc=%s calling azure-search delete", task_id, doc_id)
                try:
                    await ctx.search.delete_by_doc_ids([doc_id])
                    _LOG.info("delete[%s] doc=%s azure-search delete ok", task_id, doc_id)
                except Exception as exc:
                    azure_warning = f"{type(exc).__name__}: {exc}"
                    _LOG.warning(
                        "delete[%s] doc=%s azure-search delete FAILED, "
                        "continuing to DB delete (orphan chunks may remain): %s",
                        task_id, doc_id, azure_warning,
                    )
            else:
                _LOG.info(
                    "delete[%s] doc=%s status=%s — skipping azure delete (never indexed)",
                    task_id, doc_id, document.status,
                )

            if storage_path:
                try_unlink(storage_path)
                _LOG.info("delete[%s] doc=%s unlinked %s", task_id, doc_id, storage_path)

            try:
                async with ctx.sessionmaker() as session:
                    rowcount = await documents_repo.delete_many(session, [doc_id])
                    await session.commit()
                _LOG.info("delete[%s] doc=%s DB delete rowcount=%s", task_id, doc_id, rowcount)
                if rowcount == 0:
                    # Row was already gone — treat as success (idempotent).
                    _LOG.warning(
                        "delete[%s] doc=%s DB delete affected 0 rows; "
                        "doc was already removed by another worker?",
                        task_id, doc_id,
                    )
            except Exception as exc:
                failed = True
                err_msg = f"DB delete failed: {type(exc).__name__}: {exc}"
                _LOG.exception("delete[%s] doc=%s DB delete raised", task_id, doc_id)

        async with ctx.sessionmaker() as session:
            if failed:
                await task_files_repo.set_progress(
                    session,
                    task_id,
                    doc_id,
                    status=TaskFileStatus.FAILED.value,
                    current_step="failed",
                    error=err_msg,
                    error_details={"step": "deleting", "reason": err_msg or ""},
                )
                await tasks_repo.bump_failed(session, task_id)
            else:
                await task_files_repo.set_progress(
                    session,
                    task_id,
                    doc_id,
                    status=TaskFileStatus.COMPLETED.value,
                    current_step=ProcessingStep.COMPLETED.value,
                )
                await tasks_repo.bump_processed(session, task_id)
            await session.commit()
