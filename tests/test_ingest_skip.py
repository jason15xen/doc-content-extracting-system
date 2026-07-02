"""Integration test for the ingest pipeline's handling of a file that indexes
successfully but has unextractable (scanned / vector-outline) pages.

Drives the real pipeline (_run → _ingest_one) against a temp SQLite DB with the
extractor running for real and Azure embed/search stubbed. Asserts the agreed
behaviour: the document is indexed and counts as PROCESSED (it did ingest), but
carries a visible per-file note recording the skipped pages.
"""
import asyncio

import pymupdf
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401  (register mappers on Base.metadata)
from app.db.base import Base
from app.db.models import DocumentStatus, TaskAction, TaskFileStatus, TaskStatus
from app.pipeline.context import PipelineContext
from app.pipeline.ingest import _run
from app.repositories import documents as documents_repo
from app.repositories import task_files as task_files_repo
from app.repositories import tasks as tasks_repo
from app.settings import get_settings


def _make_mixed_pdf(path) -> None:
    """Page 1 = real text; page 2 = vector-outline (no text, many drawings)."""
    doc = pymupdf.open()
    doc.new_page().insert_text(
        (72, 72), "Real text page with well over fifty characters of content here."
    )
    page = doc.new_page()
    for k in range(150):
        page.draw_line((50, 50 + k), (550, 50 + k))
    doc.save(str(path))
    doc.close()


class _StubEmbedder:
    async def embed_many(self, texts):
        return [[0.0] * 1536 for _ in texts]

    async def aclose(self):
        pass


class _StubSearch:
    def __init__(self):
        self.upserted = []
        self.delete_calls: list[list] = []
        self.events: list[str] = []  # ordered record of upsert/delete calls

    async def upsert_chunks(self, docs):
        self.upserted.extend(docs)
        self.events.append("upsert")

    async def delete_by_doc_ids(self, doc_ids):
        self.delete_calls.append(list(doc_ids))
        self.events.append("delete")
        return 0

    async def aclose(self):
        pass


def test_unextractable_file_indexes_as_processed_with_skip_note(tmp_path):
    async def _body():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rag.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)

        pdf = tmp_path / "mixed.pdf"
        _make_mixed_pdf(pdf)
        did = "test-mixed.pdf"

        async with sm() as s:
            await documents_repo.create(
                s, doc_id=did, name="mixed.pdf", hash_="h1", description=None,
                dataset_id=None, storage_path=str(pdf), file_size=pdf.stat().st_size,
            )
            task = await tasks_repo.create(s, action=TaskAction.UPLOAD, total_files=1)
            await s.commit()
            tid = task.id
            await task_files_repo.create_many(
                s, tid,
                [{"filename": "mixed.pdf", "doc_id": did,
                  "status": TaskFileStatus.PENDING.value, "action_type": "create"}],
            )
            await s.commit()

        ctx = PipelineContext(
            settings=get_settings(),
            sessionmaker=sm,
            embedder=_StubEmbedder(),
            chatter=None,
            search=_StubSearch(),
            ingest_semaphore=asyncio.Semaphore(2),
            ocr_pool=None,
        )
        await _run(ctx, tid, [did])

        async with sm() as s:
            doc = await documents_repo.get(s, did)
            tf = await task_files_repo.get_by_task_and_doc(s, tid, did)
            tk = await tasks_repo.get(s, tid)
        await engine.dispose()
        return doc, tf, tk

    doc, tf, task = asyncio.run(_body())

    # Document indexed on its extractable page.
    assert doc.status == DocumentStatus.SUCCESS.value
    assert doc.chunk_count >= 1
    # File indexed but had unextractable pages → status is 'partial' (not a
    # clean 'completed'), with a visible note.
    assert tf.status == TaskFileStatus.PARTIAL.value
    assert tf.reason == "Skipped (no extractable text): 2"
    assert tf.error_details["skipped_pages"] == [2]
    # Counted as processed, not skipped or failed; total still adds up.
    assert task.processed_files == 1
    assert task.skipped_file_count == 0
    assert task.failed_file_count == 0
    assert task.processed_files + task.failed_file_count + task.skipped_file_count \
        == task.total_files


async def _ingest_single(tmp_path, *, filename, content_path, action):
    """Ingest one file through the real pipeline; return (doc, task_file, search)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rag.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    did = "doc-" + filename
    async with sm() as s:
        await documents_repo.create(
            s, doc_id=did, name=filename, hash_="h-" + filename, description=None,
            dataset_id=None, storage_path=str(content_path),
            file_size=content_path.stat().st_size,
        )
        task = await tasks_repo.create(s, action=TaskAction.UPLOAD, total_files=1)
        await s.commit()
        tid = task.id
        await task_files_repo.create_many(
            s, tid,
            [{"filename": filename, "doc_id": did,
              "status": TaskFileStatus.PENDING.value, "action_type": action}],
        )
        await s.commit()
    search = _StubSearch()
    ctx = PipelineContext(
        settings=get_settings(), sessionmaker=sm, embedder=_StubEmbedder(),
        chatter=None, search=search, ingest_semaphore=asyncio.Semaphore(2),
        ocr_pool=None,
    )
    await _run(ctx, tid, [did])
    async with sm() as s:
        doc = await documents_repo.get(s, did)
        tf = await task_files_repo.get_by_task_and_doc(s, tid, did)
    await engine.dispose()
    return doc, tf, search


def test_failed_update_preserves_old_chunks(tmp_path):
    """Reorder fix (#1): a failed UPDATE must NOT delete the old chunks — the
    delete happens only after extract/embed succeed, so an extraction failure
    leaves the previously-indexed document intact."""
    empty = tmp_path / "empty.txt"
    empty.write_text("")  # empty → "empty extraction" → PipelineError
    doc, tf, search = asyncio.run(
        _ingest_single(tmp_path, filename="empty.txt", content_path=empty, action="update")
    )
    assert doc.status == DocumentStatus.FAILED.value
    assert search.delete_calls == []      # old chunks NOT deleted
    assert "upsert" not in search.events


def test_successful_update_deletes_then_upserts(tmp_path):
    """Reorder fix (#1): a successful UPDATE deletes old chunks then upserts the
    new ones, in that order — both only after extraction succeeded."""
    txt = tmp_path / "doc.txt"
    txt.write_text("Plenty of real text content to drive a successful update path.")
    doc, tf, search = asyncio.run(
        _ingest_single(tmp_path, filename="doc.txt", content_path=txt, action="update")
    )
    assert doc.status == DocumentStatus.SUCCESS.value
    assert tf.status == TaskFileStatus.COMPLETED.value  # clean file → completed
    assert search.events == ["delete", "upsert"]


def test_list_documents_pagination_reports_true_total(tmp_path):
    """Pagination fix (#2): the repo returns the real total and honors offset,
    so the router can report total_count correctly and page through results."""
    async def _body():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rag.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            for i in range(5):
                await documents_repo.create(
                    s, doc_id=f"d{i}", name=f"d{i}.pdf", hash_=f"h{i}",
                    description=None, dataset_id=None,
                    storage_path=f"/tmp/d{i}", file_size=1,
                )
            await s.commit()
        async with sm() as s:
            page1, total1 = await documents_repo.list_with_dataset_names(s, limit=2, offset=0)
            last, total2 = await documents_repo.list_with_dataset_names(s, limit=2, offset=4)
        await engine.dispose()
        return len(page1), total1, len(last), total2

    n1, t1, n_last, t2 = asyncio.run(_body())
    assert (n1, t1) == (2, 5)       # first page caps at limit, total is the true count
    assert (n_last, t2) == (1, 5)   # offset past the first pages returns the tail


def test_reconcile_fails_orphaned_work_and_returns_temp_paths(tmp_path):
    """Restart-recovery fix (#2): an interrupted batch leaves a PROCESSING task
    plus PENDING docs/task_files. reconcile must fail all three (no silent
    orphans) and hand back the temp-file paths to clean up (no disk leak)."""
    async def _body():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rag.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        temp = tmp_path / "orphan.pdf"
        temp.write_text("leftover upload")
        async with sm() as s:
            await documents_repo.create(
                s, doc_id="d1", name="orphan.pdf", hash_="h1", description=None,
                dataset_id=None, storage_path=str(temp), file_size=1,
            )  # left PENDING (never started)
            task = await tasks_repo.create(s, action=TaskAction.UPLOAD, total_files=1)
            await s.commit()
            tid = task.id
            await tasks_repo.mark_started(s, tid)  # task -> processing
            await task_files_repo.create_many(
                s, tid,
                [{"filename": "orphan.pdf", "doc_id": "d1",
                  "status": TaskFileStatus.PENDING.value, "action_type": "create"}],
            )
            await s.commit()
        # A CANCELLED task with a leftover pending task_file (cancel deletes the
        # docs but not the rows) must NOT be touched by reconcile.
        async with sm() as s:
            cancelled = await tasks_repo.create(
                s, action=TaskAction.UPLOAD, total_files=1
            )
            await s.commit()
            cid = cancelled.id
            await task_files_repo.create_many(
                s, cid,
                [{"filename": "cancelled.pdf", "doc_id": "d2",
                  "status": TaskFileStatus.PENDING.value, "action_type": "create"}],
            )
            await tasks_repo.mark_cancelled(s, cid)
            await s.commit()

        async with sm() as s:
            paths = await tasks_repo.reconcile_running_tasks(s)
            await s.commit()
        async with sm() as s:
            doc = await documents_repo.get(s, "d1")
            tf = await task_files_repo.get_by_task_and_doc(s, tid, "d1")
            tk = await tasks_repo.get(s, tid)
            ctf = await task_files_repo.get_by_task_and_doc(s, cid, "d2")
            ctk = await tasks_repo.get(s, cid)
        await engine.dispose()
        return doc, tf, tk, paths, str(temp), ctf, ctk

    doc, tf, tk, paths, temp, ctf, ctk = asyncio.run(_body())
    assert tk.status == TaskStatus.FAILED.value
    assert tk.error_message == "server restart"
    assert doc.status == DocumentStatus.FAILED.value and doc.storage_path is None
    assert tf.status == TaskFileStatus.FAILED.value
    assert tf.error == "server restart"
    # Summary counter back-filled to match the failed_files list.
    assert tk.failed_file_count == 1
    assert temp in paths   # caller unlinks these leftover temp files
    # Cancelled task untouched: file stays pending, no counter bump, no relabel.
    assert ctk.status == TaskStatus.CANCELLED.value
    assert ctk.failed_file_count == 0
    assert ctf.status == TaskFileStatus.PENDING.value
    assert ctf.error is None
