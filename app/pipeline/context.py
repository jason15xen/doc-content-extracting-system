"""Runtime context shared by background pipeline tasks.

FastAPI BackgroundTasks run outside request scope, so we give them a module-level
handle to the things they need (sessionmaker, embedder, search gateway, semaphore).
Populated by app.main.lifespan at startup.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.chat import Chatter
from app.services.embeddings import Embedder
from app.services.search_index import SearchGateway
from app.settings import Settings


@dataclass
class PipelineContext:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    embedder: Embedder
    chatter: Chatter
    search: SearchGateway
    ingest_semaphore: asyncio.Semaphore


_ctx: PipelineContext | None = None


def set_context(ctx: PipelineContext) -> None:
    global _ctx
    _ctx = ctx


def get_context() -> PipelineContext:
    if _ctx is None:
        raise RuntimeError("PipelineContext not initialised")
    return _ctx


def clear_context() -> None:
    global _ctx
    _ctx = None
