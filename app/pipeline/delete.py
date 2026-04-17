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
    await _delete(ctx, task_id, doc_ids)


async def run_dataset_cascade_task(task_id: uuid.UUID, dataset_id: uuid.UUID) -> None:
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
            documents = await documents_repo.bulk_get(session, doc_ids)
            paths = [d.storage_path for d in documents if d.storage_path]

            await ctx.search.delete_by_doc_ids(doc_ids)
            await documents_repo.delete_many(session, doc_ids)
            await datasets_repo.delete_one(session, dataset_id)
            await session.commit()

            for path in paths:
                try_unlink(path)

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
    ctx: PipelineContext, task_id: uuid.UUID, doc_ids: Sequence[uuid.UUID]
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
            documents = await documents_repo.bulk_get(session, doc_ids)
            paths = [d.storage_path for d in documents if d.storage_path]

            await ctx.search.delete_by_doc_ids(doc_ids)
            await documents_repo.delete_many(session, doc_ids)
            await session.commit()

            for path in paths:
                try_unlink(path)

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
