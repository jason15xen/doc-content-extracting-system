import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Dataset


async def create(
    session: AsyncSession, *, name: str, description: str | None
) -> Dataset:
    ds = Dataset(name=name, description=description)
    session.add(ds)
    await session.flush()
    return ds


async def get(session: AsyncSession, dataset_id: uuid.UUID) -> Dataset | None:
    return await session.get(Dataset, dataset_id)


async def get_by_name(session: AsyncSession, name: str) -> Dataset | None:
    stmt = select(Dataset).where(Dataset.name == name)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_all(session: AsyncSession) -> Sequence[Dataset]:
    stmt = select(Dataset).order_by(Dataset.created_at.desc())
    return (await session.execute(stmt)).scalars().all()


async def update(
    session: AsyncSession,
    ds: Dataset,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Dataset:
    if name is not None:
        ds.name = name
    if description is not None:
        ds.description = description
    ds.updated_at = datetime.now(timezone.utc)
    return ds


async def delete_one(session: AsyncSession, dataset_id: uuid.UUID) -> int:
    stmt = delete(Dataset).where(Dataset.id == dataset_id)
    result = await session.execute(stmt)
    return result.rowcount or 0
