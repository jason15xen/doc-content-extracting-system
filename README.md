# RAG Document Ingestion & Search API

A FastAPI service that ingests documents, extracts text, chunks and embeds them via Azure OpenAI, indexes them in Azure AI Search, and serves hybrid RAG search with LLM-generated answers.

## Architecture

```
  Upload          Background pipeline              Search
  ──────          ───────────────────              ──────

  POST /documents/upload
       │
       ▼
  Save to disk ──► Extract text ──► Chunk (tiktoken) ──► Embed (Azure OpenAI)
  + SHA-256 hash   (12 formats)     800 tok / 100 overlap  text-embedding-3-small
       │                                                        │
       ▼                                                        ▼
  PostgreSQL                                              Azure AI Search
  (documents, tasks, datasets)                            (hybrid: vector + BM25)
                                                                │
                                                    POST /search│
                                                                ▼
                                                          Top-5 docs by score
                                                                │
                                                                ▼
                                                          Chat model (Azure OpenAI)
                                                                │
                                                                ▼
                                                          { answer, sources }
```

## Supported document types

| Category | Extensions |
|---|---|
| OpenXML | `.docx`, `.xlsx`, `.pptx`, `.docm`, `.xlsm`, `.pptm` |
| Legacy binary | `.doc`, `.xls`, `.ppt` (via LibreOffice) |
| Other | `.pdf`, `.txt`, `.md` |

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
- **PostgreSQL** (port 5432, internal) -- document/task/dataset metadata
- **API** (port 8889) -- FastAPI with Alembic migration on boot

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
| `POST` | `/documents/upload` | Upload files (multipart). Optional `dataset_id` form field. Returns 202 with per-file status + task IDs. |
| `GET` | `/documents` | List documents. Query params: `limit`, `offset`, `dataset_id`, `status_filter`. |
| `DELETE` | `/documents` | Delete specific documents. Body: `{"doc_ids": ["uuid", ...]}`. Deletes from DB + Azure AI Search. Returns 202 + task_id. |
| `DELETE` | `/documents/all` | Delete all documents from DB + Azure AI Search. Returns 202 + task_id. |

#### Upload response example

```json
{
  "items": [
    {"filename": "report.pdf", "status": "accepted", "document_id": "uuid", "task_id": "uuid"},
    {"filename": "bad.zip", "status": "failed", "reason": "unsupported_extension:.zip"},
    {"filename": "copy.pdf", "status": "failed", "reason": "duplicate"}
  ]
}
```

### Tasks (processing monitor)

| Method | Path | Description |
|---|---|---|
| `GET` | `/tasks` | List all tasks. Query params: `limit`, `offset`, `status`, `document_id`, `task_type`. |
| `GET` | `/tasks/{task_id}` | Get single task with progress and result. |
| `DELETE` | `/tasks/{task_id}` | Delete a single task record. |
| `DELETE` | `/tasks` | Delete all task records. |

#### Task response example

```json
{
  "id": "661a70e4-...",
  "document_id": "44b0ba9f-...",
  "task_type": "ingest",
  "status": "running",
  "stage": "embedded",
  "error_message": null,
  "progress": 75,
  "result": "processing (embedded)",
  "created_at": "2026-04-20T02:27:25Z",
  "updated_at": "2026-04-20T02:27:28Z"
}
```

Progress mapping:

| Stage | Progress |
|---|---|
| `uploaded` | 0% |
| `extracted` | 25% |
| `chunked` | 50% |
| `embedded` | 75% |
| `indexed` | 100% |

On failure, `result` contains the stage and error:
```json
{
  "status": "failed",
  "stage": "embedded",
  "progress": 75,
  "result": "failed at stage 'embedded': EmbeddingError: connection refused"
}
```

### Datasets

| Method | Path | Description |
|---|---|---|
| `POST` | `/datasets` | Create dataset. Body: `{"name": "...", "description": "..."}`. |
| `GET` | `/datasets` | List all datasets. |
| `PATCH` | `/datasets/{id}` | Update name/description. |
| `DELETE` | `/datasets/{id}` | Cascade delete: removes dataset + all its documents + AI Search chunks. Returns 202 + task_id. |

### Search

| Method | Path | Description |
|---|---|---|
| `POST` | `/search` | Hybrid RAG search with LLM answer. |

#### Request

```json
{
  "query": "What is the annual revenue?",
  "dataset_id": "uuid (optional -- omit to search all)",
  "top_k": 5
}
```

- **`dataset_id` omitted** -- searches all documents across all datasets.
- **`dataset_id` given** -- searches only documents in that dataset.

#### Response

```json
{
  "answer": "The annual revenue was $12M according to [report.pdf#3].",
  "sources": [
    {
      "doc_id": "uuid",
      "doc_name": "report.pdf",
      "score": 1.7,
      "snippet": "Revenue grew 12% year over year...",
      "chunk_indexes": [2, 3, 5]
    }
  ]
}
```

Sources are the top-5 documents ranked by cumulative score (sum of per-chunk scores across all matching chunks in that document).

### Text extraction (legacy)

| Method | Path | Description |
|---|---|---|
| `POST` | `/extract` | Synchronous text extraction. Returns plain text immediately, no DB/embedding/indexing. |

### Admin / cleanup

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/cleanup/orphan-files` | Delete files on disk with no matching DB record. |
| `POST` | `/admin/cleanup/orphan-index` | Delete AI Search chunks whose document no longer exists in DB. |

## Ingestion pipeline

When a document is uploaded, a background task runs this pipeline:

1. **Upload** -- file saved to `storage/uploads/{doc_id}{ext}`, SHA-256 computed, duplicates rejected.
2. **Extract** -- text extracted using the appropriate extractor (docx, pdf, etc.).
3. **Chunk** -- text split into 800-token chunks with 100-token overlap (tiktoken `cl100k_base`).
4. **Embed** -- each chunk embedded via Azure OpenAI `text-embedding-3-small` (1536 dims, batched 16 at a time).
5. **Index** -- chunks pushed to Azure AI Search with vector + metadata.
6. **Cleanup** -- local file deleted on success; preserved on failure for debugging.

Each stage is tracked in the `tasks` table. Poll `GET /tasks/{id}` to monitor progress.

## Database schema

### datasets
| Column | Type |
|---|---|
| id | UUID PK |
| name | TEXT UNIQUE |
| description | TEXT |
| created_at | TIMESTAMPTZ |
| updated_at | TIMESTAMPTZ |

### documents
| Column | Type |
|---|---|
| id | UUID PK |
| name | TEXT (original filename) |
| hash | CHAR(64) UNIQUE (SHA-256) |
| dataset_id | UUID FK (nullable) |
| uploaded_at | TIMESTAMPTZ |
| status | pending / processing / success / failed |
| storage_path | TEXT (null after success) |
| chunk_count | INTEGER |

### tasks
| Column | Type |
|---|---|
| id | UUID PK |
| document_id | UUID FK (nullable) |
| task_type | ingest / delete / dataset_cascade |
| status | queued / running / success / failed |
| stage | uploaded / extracted / chunked / embedded / indexed / deleted |
| error_message | TEXT |
| created_at | TIMESTAMPTZ |
| updated_at | TIMESTAMPTZ |

## Azure AI Search index

Single index (`rag-documents` by default), push model. See [index.json](index.json) for the full schema.

Key fields: `id` (chunk key: `{doc_id}_{chunk_idx}`), `doc_id`, `doc_name`, `dataset_id` (filterable), `content` (searchable, BM25), `content_vector` (1536-dim HNSW cosine), `uploaded_at`.

Search uses hybrid mode: vector similarity + BM25 keyword matching + optional semantic reranking (requires Standard S1+ tier).

## Configuration

All settings are in `.env` / `.env.dev`, read by [app/settings.py](app/settings.py). Key tuning knobs:

| Variable | Default | Description |
|---|---|---|
| `CHUNK_TOKENS` | 800 | Tokens per chunk |
| `CHUNK_OVERLAP` | 100 | Overlap between chunks |
| `EMBED_BATCH_SIZE` | 16 | Chunks per embedding API call |
| `SEARCH_TOP_K_CHUNKS` | 30 | Chunks retrieved from AI Search per query |
| `SEARCH_TOP_K_DOCS` | 5 | Distinct documents returned in response |
| `CHAT_MAX_CONTEXT_CHUNKS` | 12 | Max chunks passed to the chat model |
| `INGEST_CONCURRENCY` | 2 | Max parallel background ingest tasks |
| `ENABLE_SEMANTIC_RANKING` | true | Use semantic reranker (needs Standard S1+) |
| `ENSURE_INDEX_ON_STARTUP` | true | Create/update AI Search index on boot |

## Project structure

```
app/
  main.py                      App factory, lifespan, router registration
  settings.py                  pydantic-settings (reads .env / .env.dev)
  errors.py                    Exception classes
  deps.py                      FastAPI dependency injection providers
  extraction/                  Document text extraction (moved from original project)
    config.py                  Supported extensions, upload limit
    dispatcher.py              Extension -> Extractor routing
    schemas.py                 ExtractionResponse model
    extractors/                One module per file type
    services/libreoffice.py    soffice subprocess wrapper
  db/
    base.py                    SQLAlchemy DeclarativeBase
    session.py                 Async engine + session factory
    models.py                  Document, Task, Dataset ORM models
  repositories/                Data access layer (documents, tasks, datasets)
  schemas/                     Pydantic request/response models
  services/
    hashing.py                 Streaming SHA-256 + size-capped save
    chunker.py                 tiktoken-based text splitter
    embeddings.py              Azure OpenAI embedding client
    chat.py                    Azure OpenAI chat client (RAG answer)
    search_index.py            Azure AI Search gateway (index schema, upsert, delete, search)
    storage.py                 Local file path helpers
  pipeline/
    context.py                 Runtime context for background tasks
    ingest.py                  Upload -> extract -> chunk -> embed -> index
    delete.py                  Document/dataset deletion from DB + AI Search
  routers/
    health.py                  GET /health, GET /supported
    extract.py                 POST /extract (legacy sync extraction)
    datasets.py                Dataset CRUD + cascade delete
    documents.py               Upload, list, delete documents
    tasks.py                   Task monitoring + cleanup
    search.py                  POST /search (hybrid RAG)
    admin.py                   Orphan file/index cleanup
migrations/                    Alembic (auto-run on boot via entrypoint)
scripts/entrypoint.sh          alembic upgrade head + uvicorn
docker-compose.yml             API + PostgreSQL
Dockerfile
index.json                     Azure AI Search index schema (reference)
```

## Tests

```bash
pip install -r requirements.txt
pip install pytest pytest-asyncio httpx
python -m pytest tests/ -v
```

- `test_extract.py` -- legacy extraction endpoint (all 12 formats + multi-column PDF)
- `test_chunker.py` -- token-based chunking edge cases
- `test_hashing.py` -- streaming SHA-256 + oversize abort
- `test_search_aggregation.py` -- top-5 doc collapse by score-sum, empty results handling
