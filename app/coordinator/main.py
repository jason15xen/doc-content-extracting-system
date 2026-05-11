"""Single-URL ingest coordinator for the sharded RAG deployment.

Receives uploads on one endpoint, picks a worker for each file via
`hash(filename) % SHARD_TOTAL`, and forwards the multipart body to that
worker's existing `/documents/upload`. Workers run the unmodified RAG image —
the coordinator is the only new component.

Same filename always maps to the same worker. So:
  * No two workers process the same file.
  * Re-uploading a file lands on the same worker, where the existing
    content-hash dedup makes it a no-op.

Env vars:
  SHARD_TOTAL          number of workers (e.g. 15)
  WORKER_HOST_TEMPLATE format string, default `http://worker-{i}:8889`
  COORDINATOR_TIMEOUT  per-worker request timeout in seconds (default 600)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from typing import Annotated

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
_LOG = logging.getLogger("app.coordinator")


def _worker_index(filename: str, total: int) -> int:
    """Stable hash → worker index. SHA-256 over the filename (UTF-8) and the
    first 4 bytes interpreted as a big-endian unsigned int. Deterministic
    across processes / restarts / language runtimes — same `total`, same map."""
    digest = hashlib.sha256(filename.encode("utf-8", "surrogatepass")).digest()
    return int.from_bytes(digest[:4], "big") % total


class _Settings:
    def __init__(self) -> None:
        self.shard_total = int(os.environ.get("SHARD_TOTAL", "15"))
        if self.shard_total < 1:
            raise ValueError("SHARD_TOTAL must be >= 1")
        self.worker_url_template = os.environ.get(
            "WORKER_HOST_TEMPLATE", "http://worker-{i}:8889"
        )
        self.timeout = float(os.environ.get("COORDINATOR_TIMEOUT", "600"))

    def worker_url(self, i: int) -> str:
        return self.worker_url_template.format(i=i)


_settings = _Settings()


class ShardItem(BaseModel):
    filename: str
    status: str
    reason: str | None = None
    document_id: uuid.UUID | None = None


class ShardResult(BaseModel):
    worker_index: int
    worker_url: str
    task_id: uuid.UUID | None = None
    items: list[ShardItem] = []
    error: str | None = None


class CoordinatorUploadResponse(BaseModel):
    shards: list[ShardResult]
    total_files: int
    total_accepted: int
    total_failed: int


app = FastAPI(
    title="RAG Sharded Ingest Coordinator",
    description=(
        "Single-URL upload entry. Routes each file to one of "
        f"{_settings.shard_total} workers via SHA-256(filename) % N."
    ),
)


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "shard_total": _settings.shard_total,
        "worker_url_template": _settings.worker_url_template,
    }


@app.post("/documents/upload", response_model=CoordinatorUploadResponse)
async def upload(
    files: Annotated[list[UploadFile], File(...)],
    dataset_id: Annotated[str | None, Form()] = None,
) -> CoordinatorUploadResponse:
    """Accept a multi-file upload, route each file to its hashed worker.

    `dataset_id` (if provided) is forwarded as-is to every targeted worker.
    Each worker has its OWN local SQLite, so the dataset must already exist
    on each worker the request lands on. For batch ingest, prefer omitting
    `dataset_id` entirely.
    """
    if not files:
        raise HTTPException(status_code=422, detail="no files provided")

    # Read each upload once and bucket by target worker. UploadFile is a
    # one-shot stream — once read, it's gone — so we materialize bytes here
    # before fanning out. Memory ceiling = MAX_UPLOAD_MB × len(files).
    groups: dict[int, list[tuple[str, bytes, str | None]]] = {}
    for upload_file in files:
        filename = upload_file.filename or ""
        idx = _worker_index(filename, _settings.shard_total)
        body = await upload_file.read()
        groups.setdefault(idx, []).append(
            (filename, body, upload_file.content_type)
        )

    async with httpx.AsyncClient(timeout=_settings.timeout) as client:
        results = await asyncio.gather(
            *(
                _forward(client, idx, payloads, dataset_id)
                for idx, payloads in groups.items()
            )
        )

    total_accepted = sum(
        sum(1 for it in r.items if it.status == "accepted") for r in results
    )
    total_failed = sum(
        sum(1 for it in r.items if it.status == "failed") for r in results
    )
    return CoordinatorUploadResponse(
        shards=results,
        total_files=len(files),
        total_accepted=total_accepted,
        total_failed=total_failed,
    )


async def _forward(
    client: httpx.AsyncClient,
    worker_idx: int,
    payloads: list[tuple[str, bytes, str | None]],
    dataset_id: str | None,
) -> ShardResult:
    """POST a multi-file batch to one worker's `/documents/upload`. On any
    transport-level failure, return a `failed` ShardItem per file in the batch
    so the client sees a uniform per-file outcome."""
    url = _settings.worker_url(worker_idx) + "/documents/upload"
    files_field = [
        ("files", (filename, body, content_type or "application/octet-stream"))
        for filename, body, content_type in payloads
    ]
    data: dict[str, str] = {}
    if dataset_id:
        data["dataset_id"] = dataset_id

    try:
        resp = await client.post(url, files=files_field, data=data)
        resp.raise_for_status()
        body = resp.json()
        return ShardResult(
            worker_index=worker_idx,
            worker_url=url,
            task_id=body.get("task_id"),
            items=[ShardItem(**it) for it in (body.get("items") or [])],
        )
    except httpx.HTTPError as exc:
        _LOG.exception("forward to worker-%d failed", worker_idx)
        return ShardResult(
            worker_index=worker_idx,
            worker_url=url,
            error=f"forward_failed:{type(exc).__name__}: {exc}",
            items=[
                ShardItem(
                    filename=filename,
                    status="failed",
                    reason="worker_unreachable",
                )
                for filename, _, _ in payloads
            ],
        )


@app.get("/status")
async def status() -> dict[str, object]:
    """Aggregate task progress across every worker. Best-effort: workers that
    are slow or down show up with an `error` field instead of stats."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        results = await asyncio.gather(
            *(
                _worker_status(client, i)
                for i in range(_settings.shard_total)
            ),
            return_exceptions=False,
        )
    return {"shard_total": _settings.shard_total, "workers": results}


async def _worker_status(client: httpx.AsyncClient, worker_idx: int) -> dict[str, object]:
    url = _settings.worker_url(worker_idx) + "/tasks?limit=200"
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        body = resp.json()
        items = body.get("items") or []
        running = sum(1 for t in items if t.get("status") == "running")
        succeeded = sum(1 for t in items if t.get("status") == "success")
        failed = sum(1 for t in items if t.get("status") == "failed")
        queued = sum(1 for t in items if t.get("status") == "queued")
        return {
            "worker_index": worker_idx,
            "running": running,
            "succeeded": succeeded,
            "failed": failed,
            "queued": queued,
        }
    except httpx.HTTPError as exc:
        return {
            "worker_index": worker_idx,
            "error": f"{type(exc).__name__}: {exc}",
        }
