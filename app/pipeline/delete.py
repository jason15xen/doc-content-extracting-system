import logging
import uuid
from collections.abc import Sequence

from app.db.models import ProcessingStep, TaskFileStatus
from app.pipeline.context import PipelineContext, get_context
from app.repositories import datasets as datasets_repo
from app.repositories import documents as documents_repo
from app.repositories import task_files as task_files_repo
from app.repositories import tasks as tasks_repo
from app.services.storage import try_unlink

_LOG = logging.getLogger("app.delete")


async def run_delete_task(task_id: uuid.UUID, doc_ids: Sequence[str]) -> None:
    ctx = get_context()
    await _delete(ctx, task_id, list(doc_ids))


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
        else:
            try:
                await ctx.search.delete_by_doc_ids([doc_id])
                if storage_path:
                    try_unlink(storage_path)
                async with ctx.sessionmaker() as session:
                    await documents_repo.delete_many(session, [doc_id])
                    await session.commit()
            except Exception as exc:
                failed = True
                err_msg = f"{type(exc).__name__}: {exc}"

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
