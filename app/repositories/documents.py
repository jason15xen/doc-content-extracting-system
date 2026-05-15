from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentStatus


async def create(
    session: AsyncSession,
    *,
    doc_id: str,
    name: str,
    hash_: str,
    description: str | None,
    dataset_id,
    storage_path: str,
    file_size: int | None = None,
) -> Document:
    doc = Document(
        id=doc_id,
        name=name,
        hash=hash_,
        description=description,
        dataset_id=dataset_id,
        storage_path=storage_path,
        file_size=file_size,
        status=DocumentStatus.PENDING.value,
    )
    session.add(doc)
    await session.flush()
    return doc


async def get(session: AsyncSession, doc_id: str) -> Document | None:
    return await session.get(Document, doc_id)


async def get_by_hash(session: AsyncSession, hash_: str) -> Document | None:
    stmt = select(Document).where(Document.hash == hash_)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_paginated(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    dataset_id=None,
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


async def list_with_dataset_names(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    dataset_id=None,
    status: str | None = None,
) -> tuple[list[tuple[Document, str | None]], int]:
    """Variant of list_paginated that joins datasets to surface dataset_name
    alongside each document — needed by the sample-api DocumentInfo shape."""
    from app.db.models import Dataset

    base = select(Document, Dataset.name).join(
        Dataset, Document.dataset_id == Dataset.id, isouter=True
    )
    count_stmt = select(func.count()).select_from(Document)
    if dataset_id is not None:
        base = base.where(Document.dataset_id == dataset_id)
        count_stmt = count_stmt.where(Document.dataset_id == dataset_id)
    if status is not None:
        base = base.where(Document.status == status)
        count_stmt = count_stmt.where(Document.status == status)
    base = base.order_by(Document.uploaded_at.desc()).limit(limit).offset(offset)
    rows = list((await session.execute(base)).all())
    total = (await session.execute(count_stmt)).scalar_one()
    return [(doc, name) for doc, name in rows], total


async def replace(
    session: AsyncSession,
    doc: Document,
    *,
    name: str,
    hash_: str,
    description: str | None,
    dataset_id,
    storage_path: str,
    file_size: int | None,
) -> Document:
    """In-place update of an existing Document for the sample-api 'update'
    path: same client-supplied id, new filename/hash/content. Status resets to
    PENDING so the pipeline re-processes it."""
    doc.name = name
    doc.hash = hash_
    doc.description = description
    doc.dataset_id = dataset_id
    doc.storage_path = storage_path
    doc.file_size = file_size
    doc.status = DocumentStatus.PENDING.value
    doc.chunk_count = 0
    doc.updated_at = datetime.now(timezone.utc)
    return doc


async def bulk_get(
    session: AsyncSession, doc_ids: Sequence[str]
) -> Sequence[Document]:
    if not doc_ids:
        return []
    stmt = select(Document).where(Document.id.in_(doc_ids))
    return (await session.execute(stmt)).scalars().all()


async def delete_many(session: AsyncSession, doc_ids: Sequence[str]) -> int:
    if not doc_ids:
        return 0
    stmt = delete(Document).where(Document.id.in_(doc_ids))
    result = await session.execute(stmt)
    return result.rowcount or 0


async def list_ids_by_dataset(session: AsyncSession, dataset_id) -> list[str]:
    stmt = select(Document.id).where(Document.dataset_id == dataset_id)
    return list((await session.execute(stmt)).scalars().all())


async def list_all_ids(session: AsyncSession) -> list[str]:
    stmt = select(Document.id)
    return list((await session.execute(stmt)).scalars().all())


async def delete_all(session: AsyncSession) -> int:
    stmt = delete(Document)
    result = await session.execute(stmt)
    return result.rowcount or 0


async def move_to_dataset(
    session: AsyncSession, doc_ids: Sequence[str], target_dataset_id
) -> int:
    """Used by `DELETE /datasets/{id}?keep_documents=true` to relocate
    documents to a different dataset instead of deleting them."""
    if not doc_ids:
        return 0
    stmt = (
        update(Document)
        .where(Document.id.in_(doc_ids))
        .values(dataset_id=target_dataset_id, updated_at=datetime.now(timezone.utc))
    )
    result = await session.execute(stmt)
    return result.rowcount or 0
