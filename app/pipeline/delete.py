import uuid
from collections.abc import Sequence

from app.db.models import PipelineStage, TaskStatus
from app.pipeline.context import PipelineContext, get_context
from app.repositories import datasets as datasets_repo
from app.repositories import documents as documents_repo
from app.repositories import tasks as tasks_repo
from app.services.storage import try_unlink


async def run_delete_task(task_id: uuid.UUID, doc_ids: Sequence[uuid.UUID]) -> None:
    ctx = get_context()
    await _delete(ctx, task_id, list(doc_ids))


async def run_dataset_cascade_task(
    task_id: uuid.UUID, dataset_id: uuid.UUID
) -> None:
    ctx = get_context()
    async with ctx.sessionmaker() as session:
        task = await tasks_repo.get(session, task_id)
        if task is None:
            return
        await tasks_repo.update_stage_status(
            session, task, stage=PipelineStage.DELETED, status=TaskStatus.RUNNING
        )
        await session.commit()

        try:
            doc_ids = await documents_repo.list_ids_by_dataset(session, dataset_id)
            # Update total_items now that we know how many documents there are
            task.total_items = max(1, len(doc_ids) + 1)  # +1 for dataset row itself
            await session.commit()

            await _delete_doc_ids_with_progress(ctx, session, task, doc_ids)

            # Finally remove the dataset row itself
            await datasets_repo.delete_one(session, dataset_id)
            await tasks_repo.bump_processed(session, task)

            await tasks_repo.update_stage_status(
                session,
                task,
                stage=PipelineStage.DELETED,
                status=TaskStatus.SUCCESS,
            )
            await session.commit()
        except Exception as exc:
            await tasks_repo.update_stage_status(
                session,
                task,
                stage=PipelineStage.DELETED,
                status=TaskStatus.FAILED,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            await session.commit()


async def _delete(
    ctx: PipelineContext, task_id: uuid.UUID, doc_ids: list[uuid.UUID]
) -> None:
    async with ctx.sessionmaker() as session:
        task = await tasks_repo.get(session, task_id)
        if task is None:
            return
        await tasks_repo.update_stage_status(
            session, task, stage=PipelineStage.DELETED, status=TaskStatus.RUNNING
        )
        await session.commit()

        try:
            await _delete_doc_ids_with_progress(ctx, session, task, doc_ids)
            await tasks_repo.update_stage_status(
                session,
                task,
                stage=PipelineStage.DELETED,
                status=TaskStatus.SUCCESS,
            )
            await session.commit()
        except Exception as exc:
            await tasks_repo.update_stage_status(
                session,
                task,
                stage=PipelineStage.DELETED,
                status=TaskStatus.FAILED,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            await session.commit()


async def _delete_doc_ids_with_progress(
    ctx: PipelineContext,
    session,
    task,
    doc_ids: list[uuid.UUID],
) -> None:
    """Delete documents one-by-one, bumping processed_items after each.

    Order per doc: Azure AI Search chunks → local file → Postgres row.
    """
    for doc_id in doc_ids:
        document = await documents_repo.get(session, doc_id)
        storage_path = document.storage_path if document else None

        await ctx.search.delete_by_doc_ids([doc_id])
        if storage_path:
            try_unlink(storage_path)
        await documents_repo.delete_many(session, [doc_id])

        await tasks_repo.bump_processed(session, task)
        await session.commit()
