# RAG Document Ingestion & Search API

English | [日本語](README.ja.md)

A FastAPI service that ingests documents, extracts text (with OCR for scanned PDF pages), chunks and embeds them via Azure OpenAI, indexes them in Azure AI Search, and serves hybrid RAG search with LLM-generated answers. Multi-part questions are handled by an agentic retrieval layer: an LLM planner splits them into parallel sub-queries under a hard per-request search budget.

## Architecture

```
  Upload          Background pipeline              Query
  ──────          ───────────────────              ─────

  POST /doc
       │
       ▼
  Stream to temp ──► Extract text ──► Chunk (tiktoken) ──► Embed (Azure OpenAI)
  + SHA-256 hash     (12 formats,     800 tok / 100 overlap  text-embedding-3-small
       │              OCR fallback)                             │
       ▼                                                        ▼
  SQLite (./db/rag.db)                                    Azure AI Search
  (documents, tasks, task_files,                          (hybrid: vector + BM25
   datasets)                                               + semantic reranker)
                                                                ▲
                                                     POST /query│ 1-5 searches
                                                                │
                                        LLM planner ── single-intent → 1 hybrid search
                                             │
                                             └─ multi-intent → 3 parallel sub-queries
                                                              → judge → ≤2 follow-ups
                                                                │
                                                                ▼
                                                     Top-5 docs, top-12 chunks
                                                                │
                                                                ▼
                                                     Chat model (Azure OpenAI)
                                                                │
                                                                ▼
                                                     { answer, files, token_usage }
```

## Supported document types

| Category | Extensions |
|---|---|
| OpenXML | `.docx`, `.xlsx`, `.pptx`, `.docm`, `.xlsm`, `.pptm` |
| Legacy binary | `.doc`, `.xls`, `.ppt` (via LibreOffice) |
| Other | `.pdf`, `.txt`, `.md` |

Scanned PDF pages are detected and OCR'd with Tesseract in a bounded process pool (`OCR_ENABLED`, see Configuration). With `OCR_REJECT_SCANNED=true` the detector instead rejects any document containing a scanned page — useful for a fast text-only first pass.

## Quick start

### 1. Configure

```bash
cp .env.example .env      # or use .env.dev for development
```

Fill in the required values:

| Variable | Where to find it |
|---|---|
| `AZURE_SEARCH_ENDPOINT` | Azure Portal > Search service > Overview |
| `AZURE_SEARCH_API_KEY` | Azure Portal > Search service > Keys (admin key) |
| `AZURE_OPENAI_ENDPOINT` | Azure Portal > OpenAI resource > Keys and Endpoint |
| `AZURE_OPENAI_API_KEY` | Same as above |
| `AZURE_OPENAI_DEPLOYMENT` | Azure OpenAI Studio > Deployments (chat model) |
| `AZURE_OPENAI_EMBEDDING_ENDPOINT` | Embedding resource endpoint (if separate) |
| `AZURE_OPENAI_EMBEDDING_API_KEY` | Embedding resource key (if separate) |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | Embedding deployment name |

If chat and embedding share the same Azure OpenAI resource, leave `AZURE_OPENAI_EMBEDDING_ENDPOINT` and `AZURE_OPENAI_EMBEDDING_API_KEY` blank -- the app falls back to the chat endpoint/key.

### 2. Run

```bash
docker compose up --build
```

This starts:
- **API** (port 8889) -- FastAPI with Alembic migration on boot. Metadata lives in a local SQLite file at `./db/rag.db` (bind-mounted to `/srv/db/rag.db` inside the container).

### 3. Use

Open Swagger UI at `http://localhost:8889/docs`.

## API endpoints

### Health & info

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | `{"status": "ok"}` |
| `GET` | `/supported` | List of accepted file extensions |

### Documents

| Method | Path | Description |
|---|---|---|
| `POST` | `/doc` | Upload files (multipart) with an `items` JSON form field carrying client-supplied document IDs. Optional `dataset` and `description` form fields. Returns 202 with a batch `task_id` + `status_url`. |
| `GET` | `/doc` | List documents. Query params: `limit`, `offset`, `dataset`. |
| `DELETE` | `/doc` | Delete specific documents. Body: `{"doc_ids": [...]}`. Deletes from DB + Azure AI Search. Returns 202 + task_id. |
| `DELETE` | `/doc/all` | Delete all documents from DB + Azure AI Search. Returns 202 + task_id. |

Per-file upload outcomes (sample-api semantics): unchanged re-uploads (same ID, filename, and content) are **skipped**; re-uploads of an existing ID with new content are **updated** in place, keeping document metadata; unsupported types, oversize files, and save/commit errors are reported in `failed_files` with a reason. A batch that conflicts with an active task on the same documents is rejected with **409**.

#### Upload response (202)

```json
{
  "task_id": "uuid",
  "message": "2 files accepted, 1 skipped",
  "status_url": "/doc/status/uuid",
  "total_files": 3,
  "skipped_files": [{"filename": "same.pdf", "docId": "doc-1", "reason": "unchanged (same ID, filename, and content)"}],
  "failed_files": [],
  "updated_files": ["changed.pdf"],
  "dataset": null
}
```

### Tasks (processing monitor)

| Method | Path | Description |
|---|---|---|
| `GET` | `/doc/status/{task_id}` | Batch task status with per-file progress (`files[]`: status, current_step, actionType, reason/error). |
| `GET` | `/doc/tasks` | List tasks. Query params: `status_filter`, `limit`. |
| `POST` | `/doc/tasks/{task_id}/cancel` | Cancel a queued/running task. |
| `DELETE` | `/doc/tasks` | Clear finished task records. |

A task is one **batch** (upload or delete), with counters (`total_files`, `processed_files`, `failed_file_count`, `skipped_file_count`) and a `files[]` array tracking each file through the pipeline steps. The task completes when the batch finishes; individual file failures are listed in `failed_files` without failing the whole task.

### Datasets

| Method | Path | Description |
|---|---|---|
| `POST` | `/datasets` | Create dataset. Body: `{"name": "...", "description": "..."}`. |
| `GET` | `/datasets` | List all datasets. |
| `GET` | `/datasets/{id}` | Get one dataset. |
| `GET` | `/datasets/{id}/documents` | List documents in the dataset. |
| `PUT` | `/datasets/{id}` | Update name/description (form fields). |
| `DELETE` | `/datasets/{id}` | Cascade delete: removes dataset + all its documents + AI Search chunks. Returns 202 + task_id. Guarded by the 409 active-task lock; the default dataset is protected. |

### Query (RAG search)

| Method | Path | Description |
|---|---|---|
| `POST` | `/query` | Agentic hybrid RAG search with LLM answer. |

#### Request

```json
{
  "query": "Compare the seismic design requirements for structures and for mechanical components.",
  "dataset": "uuid (optional -- omit to search all datasets)",
  "use_cache": true
}
```

#### Response

```json
{
  "query": "…",
  "answer": "… cited inline as [doc_name#chunk_index] …",
  "from_cache": false,
  "files": [
    {
      "id": "doc-uuid",
      "name": "report.pdf",
      "relevance_score": 100.0,
      "dataset_id": "uuid",
      "dataset_name": "contracts"
    }
  ],
  "dataset": null,
  "token_usage": {"prompt_tokens": 9676, "completion_tokens": 663, "total_tokens": 10339}
}
```

`relevance_score` is a 0-100 percentage normalized against the best-matching document in this result set (top document is always 100). `token_usage` covers the whole request: query planning + judging + answer generation.

#### Agentic retrieval

Each query is first classified by an LLM planner ([app/services/query_planner.py](app/services/query_planner.py)):

- **Single-intent** (most factual questions): exactly **1** hybrid search — the classic path, no extra index load.
- **Multi-intent** (comparisons, multi-part questions): the planner writes **3** focused sub-queries which run in parallel; a judge call then either accepts the results or requests up to **2** follow-up searches. Hard cap: **5 index searches per request**, enforced in code regardless of model output ([app/services/retrieval.py](app/services/retrieval.py)).

Rows from all searches are deduped by chunk ID (best score wins) before the document ranking below. The agentic layer degrades instead of failing: planner error or timeout → single classic search; judge error → answer from round 1; one sub-query failing → the others are still used; all follow-ups failing → answer from round-1 results. Every request logs its plan: `N search(es), single_intent=…, subqueries=…, followups=…`.

After retrieval: chunks are grouped by document, each document scored by its best chunk, the top `SEARCH_TOP_K_DOCS` documents kept, and their top `CHAT_MAX_CONTEXT_CHUNKS` chunks (by score) sent to the chat model with instructions to answer only from the sources.

Query embedding runs on a priority lane: it never queues behind bulk-ingest embedding batches and fails fast (503) under heavy Azure throttling instead of hanging.

### Text extraction (legacy)

| Method | Path | Description |
|---|---|---|
| `POST` | `/extract` | Synchronous text extraction. Returns plain text immediately, no DB/embedding/indexing. |

### Admin / cleanup

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/cleanup/orphan-files` | Delete files on disk with no matching DB record. |
| `POST` | `/admin/cleanup/orphan-index` | Delete AI Search chunks whose document no longer exists in DB. |
| `GET` | `/admin/logs` | List application log files. |
| `GET` | `/admin/logs/{filename}` | Download one log file. |

## Ingestion pipeline

When a batch is uploaded, a background task runs this pipeline per file:

1. **Upload** -- file streamed to an OS temp file (via `tempfile.mkstemp`); source bytes are never written under `storage/uploads/`. SHA-256 computed during the stream.
2. **Extract** -- text extracted using the appropriate extractor (docx, pdf, etc.). Scanned PDF pages are detected and OCR'd (Tesseract process pool) unless OCR is disabled.
3. **Chunk** -- text split into 800-token chunks with 100-token overlap (tiktoken `cl100k_base`).
4. **Embed** -- chunks embedded via Azure OpenAI (1536 dims, batched, bounded by `EMBED_MAX_INFLIGHT_BATCHES`).
5. **Index** -- chunks pushed to Azure AI Search with vector + metadata (bounded by `SEARCH_MAX_INFLIGHT_UPLOADS`).
6. **Cleanup** -- temp file is always unlinked, whether the pipeline succeeded or failed for that document.

**Batch ordering** -- within one upload, files are processed largest-first (LPT scheduling), so the biggest document starts at `t=0` instead of running as a serial tail.

**Task vs. file status** -- the batch task is marked completed when the batch runs to completion; per-file failures stay visible in the task's `failed_files` and in `GET /doc`.

## Database schema

SQLite via SQLAlchemy + Alembic ([app/db/models.py](app/db/models.py)).

| Table | Purpose | Key columns |
|---|---|---|
| `datasets` | Grouping | `id` (UUID), `name` (unique), `description`, timestamps |
| `documents` | One row per document | `id` (TEXT, client-supplied), `name`, `hash` (SHA-256), `description`, `dataset_id` FK, `status`, `storage_path`, `file_size`, `chunk_count`, timestamps |
| `tasks` | One row per batch (upload/delete) | `id` (UUID), `action`, `status`, file counters, `current_file`, `current_step`, `error_message`, timing columns |
| `task_files` | Per-file progress within a batch | `task_id` FK, `filename`, `doc_id`, `status`, `current_step`, `action_type`, `reason`, `error`, `error_details` |

## Azure AI Search index

Single index (`rag-documents` by default), push model. The schema is defined in code at [app/services/search_index.py](app/services/search_index.py) (`build_index()`); the app creates or updates it on startup when `ENSURE_INDEX_ON_STARTUP=true`. To get a JSON dump for manual PUT, run `python -m scripts.export_index_schema`.

Key fields: `id` (chunk key: `{doc_id}_{chunk_idx}`), `doc_id`, `doc_name`, `dataset_id` (filterable), `content` (searchable, BM25), `content_vector` (1536-dim HNSW cosine, hidden), `uploaded_at`.

Each search is hybrid: vector similarity + BM25 keyword matching, fused with RRF, plus optional semantic reranking (requires Standard S1+ tier). The agentic layer issues 1-5 such searches per `/query` request.

## Configuration

All settings are in `.env` / `.env.dev`, read by [app/settings.py](app/settings.py). Key tuning knobs:

| Variable | Default | Description |
|---|---|---|
| `CHUNK_TOKENS` | 800 | Tokens per chunk |
| `CHUNK_OVERLAP` | 100 | Overlap between chunks |
| `EMBED_BATCH_SIZE` | 16 | Chunks per embedding API call |
| `SEARCH_TOP_K_CHUNKS` | 30 | Chunks retrieved per classic (single-intent) search |
| `SEARCH_TOP_K_DOCS` | 5 | Distinct documents returned in response |
| `CHAT_MAX_CONTEXT_CHUNKS` | 12 | Max chunks passed to the chat model |
| `AGENTIC_SEARCH_ENABLED` | true | LLM query planner on `/query` (false = classic single search) |
| `AGENTIC_MAX_SUBQUERIES` | 3 | Parallel sub-queries for a multi-intent question |
| `AGENTIC_MAX_SEARCHES` | 5 | Hard cap on index searches per request (sub-queries + follow-ups) |
| `AGENTIC_SUBQUERY_TOP_K` | 15 | Chunks retrieved per sub-query (0 = `SEARCH_TOP_K_CHUNKS`) |
| `AGENTIC_LLM_TIMEOUT_S` | 20 | Planner/judge call timeout; on timeout the query degrades to a classic search |
| `INGEST_CONCURRENCY` | 2 | Max parallel background ingest tasks |
| `EMBED_MAX_INFLIGHT_BATCHES` | 4 | Cap on concurrent embedding batches (Azure TPM guard) |
| `SEARCH_MAX_INFLIGHT_UPLOADS` | 4 | Cap on concurrent index upload batches |
| `OCR_ENABLED` | true | OCR scanned PDF pages (Tesseract) |
| `OCR_WORKERS` | 6 | OCR process pool size |
| `OCR_REJECT_SCANNED` | false | Reject docs containing scanned pages instead of OCR-ing |
| `ENABLE_SEMANTIC_RANKING` | true | Use semantic reranker (needs Standard S1+) |
| `ENSURE_INDEX_ON_STARTUP` | true | Create/update AI Search index on boot |

## Project structure

```
app/
  main.py                      App factory, lifespan, router registration
  settings.py                  pydantic-settings (reads .env / .env.dev)
  errors.py                    Exception classes
  deps.py                      FastAPI dependency injection providers
  extraction/                  Document text extraction
    config.py                  Supported extensions, upload limit
    dispatcher.py              Extension -> Extractor routing
    scan_detect.py             Scanned-page detection for PDFs
    ocr.py                     Tesseract OCR (process pool)
    schemas.py                 ExtractionResponse model
    extractors/                One module per file type
    services/libreoffice.py    soffice subprocess wrapper
  db/
    base.py                    SQLAlchemy DeclarativeBase
    session.py                 Async engine + session factory
    models.py                  Document, Task, TaskFile, Dataset ORM models
  repositories/                Data access layer (documents, tasks, datasets)
  schemas/                     Pydantic request/response models
  services/
    hashing.py                 Streaming SHA-256 + size-capped save
    chunker.py                 tiktoken-based text splitter
    embeddings.py              Azure OpenAI embedding client (priority query lane)
    chat.py                    Azure OpenAI chat client (RAG answer)
    query_planner.py           Agentic-search LLM planner + judge (JSON mode)
    retrieval.py               Agentic retrieval orchestration (fan-out, dedupe, search budget)
    search_index.py            Azure AI Search gateway (index schema, upsert, delete, search)
    logging_setup.py           Rotating file logging under storage/logs/
    storage.py                 Local file path helpers
  pipeline/
    context.py                 Runtime context for background tasks
    ingest.py                  Upload -> extract -> chunk -> embed -> index
    delete.py                  Document/dataset deletion from DB + AI Search
  routers/
    health.py                  GET /health, GET /supported
    extract.py                 POST /extract (legacy sync extraction)
    datasets.py                Dataset CRUD + cascade delete
    documents.py               POST/GET/DELETE /doc (upload, list, delete)
    tasks.py                   GET /doc/status/{id}, /doc/tasks, cancel, clear
    query.py                   POST /query (agentic hybrid RAG)
    admin.py                   Orphan cleanup + log access
migrations/                    Alembic (auto-run on boot via entrypoint)
scripts/
  entrypoint.sh                alembic upgrade head + uvicorn
  export_index_schema.py       Dump build_index() output as JSON (for manual PUT)
  diagnose_pdf.py              PDF extraction/scan-detection debugging helper
docker-compose.yml             API (SQLite at ./db/rag.db, no external DB service)
Dockerfile
```

## Tests

```bash
pip install -r requirements-dev.txt   # includes runtime deps + pytest + fixture generators
python -m pytest tests/ -v
```

- `test_extract.py` -- extraction endpoint (all 12 formats + multi-column PDF)
- `test_chunker.py`, `test_chunk_key.py` -- token-based chunking + chunk key edge cases
- `test_hashing.py` -- streaming SHA-256 + oversize abort
- `test_upload_api.py`, `test_ingest_skip.py` -- upload semantics (skip/update/failed, 409 lock)
- `test_embed_query.py` -- priority embedding lane (semaphore bypass, fail-fast retry)
- `test_search_index.py` -- index schema and gateway behavior
- `test_search_aggregation.py` -- top-K doc collapse + relevance normalization in `/query`
- `test_agentic_search.py` -- agentic retrieval: search budget (1/3/5), dedupe, prompt parsing, degradation paths
- `test_log_filter.py` -- access-log poll filtering
