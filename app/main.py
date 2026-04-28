import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.db.session import build_engine, build_sessionmaker
from app.pipeline import context as pipeline_context
from app.pipeline.context import PipelineContext
from app.repositories import tasks as tasks_repo
from app.routers import admin, datasets, documents, extract, health, search, tasks
from app.services.chat import Chatter
from app.services.embeddings import Embedder
from app.services.logging_setup import setup_file_logging
from app.services.search_index import SearchGateway
from app.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    try:
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        setup_file_logging(settings.logs_dir)
        logging.getLogger("app").info(
            "file logging initialized at %s", settings.logs_dir
        )
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
            # Don't block startup for dev — but log so the cause is visible.
            logging.getLogger("app").exception(
                "ensure_index failed at startup (continuing without aborting)"
            )

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
    # unreachable database at startup (e.g. during tests with DI overrides),
    # but log the cause so silent boot-time DB issues are recoverable.
    try:
        async with sessionmaker() as session:
            await tasks_repo.reconcile_running_tasks(session)
            await session.commit()
    except Exception:
        logging.getLogger("app").exception(
            "reconcile_running_tasks failed at startup"
        )

    try:
        yield
    finally:
        await search_gw.aclose()
        await embedder.aclose()
        await chatter.aclose()
        await engine.dispose()
        pipeline_context.clear_context()


app = FastAPI(
    title="RAG Ingestion & Search API",
    lifespan=lifespan,
    docs_url=None,  # replaced below with a CDN-served newer Swagger UI
)


_REQUEST_LOG = logging.getLogger("app.request")


class _RequestTimingMiddleware(BaseHTTPMiddleware):
    """Log every API call with method, path, status, duration. Skips static
    UI assets and health pings to keep the log signal-to-noise high."""

    # Exact-match paths — won't accidentally swallow look-alike routes such
    # as `/health-check` or `/docs-archive` if those are added later. The
    # `/favicon.ico` entry suppresses 404 noise from browser auto-fetches
    # whenever someone opens `/ui`.
    _SKIP_EXACT = frozenset(
        {"/ui", "/openapi.json", "/docs", "/health", "/favicon.ico"}
    )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self._SKIP_EXACT:
            return await call_next(request)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            _REQUEST_LOG.exception(
                "%s %s -> 500 in %.1fms", request.method, path, elapsed_ms
            )
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        _REQUEST_LOG.info(
            "%s %s -> %d in %.1fms",
            request.method,
            path,
            response.status_code,
            elapsed_ms,
        )
        return response


app.add_middleware(_RequestTimingMiddleware)


_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/ui", include_in_schema=False)
async def test_ui():
    """Developer-friendly test UI with native multi-file upload, task
    progress tracking, dataset management, and a search form."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    """Override the bundled Swagger UI with a recent CDN version that renders
    ``array<file>`` as a single multi-select file picker instead of one picker
    per array item."""
    return get_swagger_ui_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=f"{app.title} - Swagger UI",
        swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui-bundle.js",
        swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui.css",
    )


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
