import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentStatus


async def create(
    session: AsyncSession,
    *,
    name: str,
    hash_: str,
    dataset_id: uuid.UUID | None,
    storage_path: str,
) -> Document:
    doc = Document(
        name=name,
        hash=hash_,
        dataset_id=dataset_id,
        storage_path=storage_path,
        status=DocumentStatus.PENDING.value,
    )
    session.add(doc)
    await session.flush()
    return doc


async def get(session: AsyncSession, doc_id: uuid.UUID) -> Document | None:
    return await session.get(Document, doc_id)


async def get_by_hash(session: AsyncSession, hash_: str) -> Document | None:
    stmt = select(Document).where(Document.hash == hash_)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_paginated(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    dataset_id: uuid.UUID | None = None,
    status: str | None = None,
) -> tuple[Sequence[Document], int]:
    base = select(Document)
    count_stmt = select(func.count()).select_from(Document)
    if dataset_id is not None:
        base = base.where(Document.dataset_id == dataset_id)
        count_stmt = count_stmt.where(Document.dataset_id == dataset_id)
    if status is not None:
        base = base.where(Document.status == status)
        count_stmt = count_stmt.where(Document.status == status)
    base = base.order_by(Document.uploaded_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(base)).scalars().all()
    total = (await session.execute(count_stmt)).scalar_one()
    return rows, total


async def set_status(
    session: AsyncSession, doc: Document, status: DocumentStatus
) -> None:
    doc.status = status.value


async def mark_success(session: AsyncSession, doc: Document, chunk_count: int) -> None:
    doc.status = DocumentStatus.SUCCESS.value
    doc.chunk_count = chunk_count
    doc.storage_path = None


async def bulk_get(
    session: AsyncSession, doc_ids: Sequence[uuid.UUID]
) -> Sequence[Document]:
    if not doc_ids:
        return []
    stmt = select(Document).where(Document.id.in_(doc_ids))
    return (await session.execute(stmt)).scalars().all()


async def delete_many(session: AsyncSession, doc_ids: Sequence[uuid.UUID]) -> int:
    if not doc_ids:
        return 0
    stmt = delete(Document).where(Document.id.in_(doc_ids))
    result = await session.execute(stmt)
    return result.rowcount or 0


async def list_ids_by_dataset(
    session: AsyncSession, dataset_id: uuid.UUID
) -> list[uuid.UUID]:
    stmt = select(Document.id).where(Document.dataset_id == dataset_id)
    return list((await session.execute(stmt)).scalars().all())


async def list_all_ids(session: AsyncSession) -> list[uuid.UUID]:
    stmt = select(Document.id)
    return list((await session.execute(stmt)).scalars().all())
