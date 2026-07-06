"""Upload-API behavior: metadata preservation on re-upload, and the 409
active-task lock (sample-api parity). Real temp SQLite; pipeline stubbed."""
import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401  (register mappers)
from app import deps
from app.db.base import Base
from app.db.models import Dataset, DocumentStatus, TaskAction
from app.main import app
from app.repositories import documents as documents_repo
from app.repositories import tasks as tasks_repo
from app.settings import get_settings


@pytest.fixture()
def client(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rag.db'}")

    async def _create_all():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_all())
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def _session_dep():
        async with sm() as session:
            yield session

    class _Ctx:
        settings = get_settings()
        sessionmaker = sm
        ocr_pool = None

    app.dependency_overrides[deps.get_session] = _session_dep
    app.dependency_overrides[deps.get_pipeline_context] = lambda: _Ctx()
    with TestClient(app) as c:
        c.sessionmaker = sm  # handed to tests for direct DB reads/seeding
        yield c
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def _run(coro):
    return asyncio.run(coro)


async def _seed_doc(sm, doc_id: str, description: str | None):
    """Existing SUCCESS doc inside a dataset — the re-upload/update target."""
    async with sm() as s:
        ds = Dataset(name=f"ds-{uuid.uuid4()}", description=None)
        s.add(ds)
        await s.flush()
        await documents_repo.create(
            s, doc_id=doc_id, name="a.txt", hash_=f"old-{doc_id}",
            description=description, dataset_id=ds.id,
            storage_path=None, file_size=1,
        )
        doc = await documents_repo.get(s, doc_id)
        doc.status = DocumentStatus.SUCCESS.value
        await s.commit()
        return ds.id


def _upload(client, doc_id: str, content: bytes, **form):
    items = json.dumps(
        [{"clientFileId": "a.txt", "id": doc_id, "fileName": "a.txt"}]
    )
    return client.post(
        "/doc",
        files=[("files", ("a.txt", content))],
        data={"items": items, **form},
    )


async def _complete_all_tasks(sm):
    """Finish the task the previous upload left behind, freeing the 409 lock."""
    async with sm() as s:
        while True:
            active = await tasks_repo.get_active(s)
            if active is None:
                break
            await tasks_repo.mark_completed(s, active.id)
        await s.commit()


def test_reupload_without_metadata_keeps_existing(client):
    ds_id = _run(_seed_doc(client.sessionmaker, "doc-1", "keep me"))

    r = _upload(client, "doc-1", b"new content v2")  # no dataset/description
    assert r.status_code == 202, r.text

    async def _get():
        async with client.sessionmaker() as s:
            return await documents_repo.get(s, "doc-1")

    doc = _run(_get())
    assert doc.dataset_id == ds_id            # dataset preserved
    assert doc.description == "keep me"       # description preserved
    assert doc.hash != "old-doc-1"            # content update went through


def test_reupload_with_new_description_still_updates(client):
    _run(_seed_doc(client.sessionmaker, "doc-2", "old text"))

    r = _upload(client, "doc-2", b"newer content", description="new text")
    assert r.status_code == 202, r.text

    async def _get():
        async with client.sessionmaker() as s:
            return await documents_repo.get(s, "doc-2")

    assert _run(_get()).description == "new text"


def test_upload_and_delete_rejected_while_task_active(client):
    async def _make_active():
        async with client.sessionmaker() as s:
            task = await tasks_repo.create(
                s, action=TaskAction.UPLOAD, total_files=3
            )
            await tasks_repo.mark_started(s, task.id)
            await s.commit()
            return task.id

    active_id = _run(_make_active())

    # Upload blocked with sample-api's 409 shape.
    r = _upload(client, "doc-3", b"whatever")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "already running" in detail["message"]
    assert detail["active_task"]["task_id"] == str(active_id)
    assert detail["active_task"]["status_url"] == f"/doc/status/{active_id}"

    # Bulk delete blocked too.
    r = client.request("DELETE", "/doc", json={"doc_ids": ["x"]})
    assert r.status_code == 409

    # Dataset cascade-delete blocked as well (sample-api guards it too).
    r = client.delete(f"/datasets/{uuid.uuid4()}")
    assert r.status_code == 409

    # Lock releases once the task finishes.
    _run(_complete_all_tasks(client.sessionmaker))
    r = _upload(client, "doc-3", b"whatever")
    assert r.status_code == 202, r.text
