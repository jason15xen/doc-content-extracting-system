from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.pipeline.context import PipelineContext
from app.services.chat import Chatter
from app.services.embeddings import Embedder
from app.services.search_index import SearchGateway
from app.settings import Settings


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_pipeline_context(request: Request) -> PipelineContext:
    return request.app.state.pipeline


async def get_session(
    ctx: PipelineContext = Depends(get_pipeline_context),
) -> AsyncIterator[AsyncSession]:
    async with ctx.sessionmaker() as session:
        yield session


def get_search(
    ctx: PipelineContext = Depends(get_pipeline_context),
) -> SearchGateway:
    return ctx.search


def get_embedder(
    ctx: PipelineContext = Depends(get_pipeline_context),
) -> Embedder:
    return ctx.embedder


def get_chatter(
    ctx: PipelineContext = Depends(get_pipeline_context),
) -> Chatter:
    return ctx.chatter
