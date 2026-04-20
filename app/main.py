import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.db.session import build_engine, build_sessionmaker
from app.pipeline import context as pipeline_context
from app.pipeline.context import PipelineContext
from app.repositories import tasks as tasks_repo
from app.routers import admin, datasets, documents, extract, health, search, tasks
from app.services.chat import Chatter
from app.services.embeddings import Embedder
from app.services.search_index import SearchGateway
from app.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    try:
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)

    search_gw = SearchGateway(settings)
    embedder = Embedder(settings)
    chatter = Chatter(settings)

    if settings.ensure_index_on_startup and settings.azure_search_endpoint:
        try:
            await search_gw.ensure_index()
        except Exception:
            # Surface through logs at container level; do not block startup for dev.
            pass

    ctx = PipelineContext(
        settings=settings,
        sessionmaker=sessionmaker,
        embedder=embedder,
        chatter=chatter,
        search=search_gw,
        ingest_semaphore=asyncio.Semaphore(settings.ingest_concurrency),
    )
    pipeline_context.set_context(ctx)

    app.state.settings = settings
    app.state.pipeline = ctx

    # Reconcile any `running` tasks left over from a prior crash. Tolerant of
    # unreachable database at startup (e.g. during tests with DI overrides).
    try:
        async with sessionmaker() as session:
            await tasks_repo.reconcile_running_tasks(session)
            await session.commit()
    except Exception:
        pass

    try:
        yield
    finally:
        await search_gw.aclose()
        await embedder.aclose()
        await chatter.aclose()
        await engine.dispose()
        pipeline_context.clear_context()


app = FastAPI(title="RAG Ingestion & Search API", lifespan=lifespan)

app.include_router(health.router)
app.include_router(extract.router)
app.include_router(datasets.router)
app.include_router(documents.router)
app.include_router(tasks.router)
app.include_router(search.router)
app.include_router(admin.router)


def _patch_upload_schemas(schema: dict[str, Any]) -> None:
    """Fix Swagger UI file-upload rendering.

    FastAPI + Pydantic v2 emits ``contentMediaType: application/octet-stream``
    for UploadFile fields, but Swagger UI only shows file-picker widgets when
    the item schema is ``{type: string, format: binary}``.
    """
    for obj in (schema.get("components", {}).get("schemas", {}) or {}).values():
        for prop in (obj.get("properties") or {}).values():
            items = prop.get("items", prop)
            if items.pop("contentMediaType", None):
                items["format"] = "binary"


_original_openapi = app.openapi


def _patched_openapi() -> dict[str, Any]:
    schema = _original_openapi()
    _patch_upload_schemas(schema)
    return schema


app.openapi = _patched_openapi  # type: ignore[method-assign]
