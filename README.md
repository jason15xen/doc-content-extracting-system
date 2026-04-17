# Document Content Extraction API

A FastAPI service that extracts the full textual content of office documents and returns it as **plain text**.

## Supported file types

| Category | Extensions |
| --- | --- |
| OpenXML | `.docx`, `.xlsx`, `.pptx`, `.docm`, `.xlsm`, `.pptm` |
| Legacy binary | `.doc`, `.xls`, `.ppt` |
| Other | `.pdf`, `.txt`, `.md` |

## How it works

```
         POST /extract  (multipart: file=@doc.xlsx)
                │
                ▼
        dispatcher (pick extractor by extension)
                │
  ┌──────────────┴──────────────────────────────────┐
  │                                                  │
 .docx/.docm → DocxExtractor    (python-docx — body + headers/footers + tables)
 .xlsx/.xlsm → XlsxExtractor    (openpyxl, data_only=True — evaluated cell values)
 .pptx/.pptm → PptxExtractor    (python-pptx — shapes, tables, speaker notes)
 .doc/.xls/.ppt → LegacyExtractor
                 └─ soffice --headless --convert-to {docx|xlsx|pptx}
                    then delegates to the matching OpenXML extractor
 .pdf        → PdfExtractor     (pymupdf4llm — column-aware Markdown per page)
 .txt/.md    → TextExtractor    (utf-8-sig → utf-8 → utf-16 → latin-1 fallback)
```

Each extractor internally builds a list of structured elements in reading order; the base class linearizes them into a single `plain_text` string that is returned to the caller.

### Key files

- [app/main.py](app/main.py) — FastAPI endpoints (`/extract`, `/health`, `/supported`)
- [app/dispatcher.py](app/dispatcher.py) — picks the right extractor by extension
- [app/extractors/](app/extractors/) — one module per file type
- [app/extractors/base.py](app/extractors/base.py) — shared `Extractor` base + `elements_to_plain_text`
- [app/services/libreoffice.py](app/services/libreoffice.py) — `soffice` subprocess wrapper (used for legacy formats)
- [app/config.py](app/config.py) — supported extensions, upload limit, soffice timeout

## Running

### Docker (recommended — bundles LibreOffice)

```bash
docker build -t doc-extract .
docker run --rm -p 8889:8889 doc-extract
```

### Local

You need Python 3.10+ and, if you want to handle `.doc/.xls/.ppt`, LibreOffice installed (`soffice` on `PATH`).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8889
```

## Endpoints

### `GET /health`

```json
{"status": "ok"}
```

### `GET /supported`

Returns the list of accepted extensions.

```json
{"extensions": [".doc", ".docm", ".docx", ".md", ".pdf", ".ppt", ".pptm", ".pptx", ".txt", ".xls", ".xlsm", ".xlsx"]}
```

### `POST /extract`

Upload one or more files as `multipart/form-data` under the field name `files`. Response is always a JSON **array** — one item per input file, in the order they were sent.

```bash
# Single file (response is a 1-item list)
curl -F 'files=@/path/to/report.xlsx' http://localhost:8889/extract

# Multiple files in one request
curl -F 'files=@a.docx' -F 'files=@b.pdf' -F 'files=@c.xlsx' http://localhost:8889/extract
```

All files are extracted **concurrently** via the threadpool. **Per-file errors do not fail the whole request** — if one file is unsupported or fails to parse, that item's entry in the response carries an `error` field and the other files still succeed.

**Status codes:**

| Code | Meaning |
| --- | --- |
| `200` | Request processed. Individual per-file failures (unsupported extension, oversize, parse error) appear as `error` fields inside the response list — they do **not** flip the overall status. |
| `422` | Request itself was malformed (e.g. missing `files` field). |

## Response type

The envelope is always a **list** — even when you send a single file (you get a 1-item list back). Each item is either a **success** or an **error**, distinguished by the presence of the `error` field.

Per-item model defined in [app/schemas.py](app/schemas.py):

```python
class ExtractionResponse(BaseModel):
    filename: str                  # original uploaded filename (always present)
    file_type: str | None = None   # only on success
    plain_text: str | None = None  # only on success
    error: str | None = None       # only on failure
```

`None` fields are omitted from the serialized JSON (`response_model_exclude_none=True`), so clients see a clean shape:

- **Success item** — `{filename, file_type, plain_text}`
- **Error item** — `{filename, error}`

Equivalent TypeScript:

```ts
type ExtractionItem =
  | { filename: string; file_type: string; plain_text: string }     // success
  | { filename: string; error: string };                            // failure

type ExtractResponse = ExtractionItem[];
```

Distinguish success vs. failure with `"error" in item` (or in TS, with a discriminated union guard).

### Example responses

**Single file** — uploading `report.xlsx`:

```json
[
  {
    "filename": "report.xlsx",
    "file_type": "xlsx",
    "plain_text": "Data\nname\tvalue\nalpha\t1\nbeta\t2"
  }
]
```

**Multiple files** — uploading `memo.docx` + `guide.pdf` + `notes.md`:

```json
[
  {
    "filename": "memo.docx",
    "file_type": "docx",
    "plain_text": "Company header\nPage 1\nIntroduction\nname\tvalue\nalpha\t1"
  },
  {
    "filename": "guide.pdf",
    "file_type": "pdf",
    "plain_text": "[Page 1]\nHello pdf world\n[Page 2]\nSecond page body"
  },
  {
    "filename": "notes.md",
    "file_type": "md",
    "plain_text": "# Heading\n\nBody paragraph."
  }
]
```

**Mixed batch with one bad file** — uploading `good.docx` + `bad.zip` + `too_big.pdf`:

```json
[
  {
    "filename": "good.docx",
    "file_type": "docx",
    "plain_text": "Hello docx world\nA1\tB1\nA2\tB2"
  },
  {
    "filename": "bad.zip",
    "error": "Unsupported file extension: .zip"
  },
  {
    "filename": "too_big.pdf",
    "error": "File exceeds 100 MB limit"
  }
]
```

HTTP status is still `200` — the bad files are reported per-item, and `good.docx` is processed normally.

**Per-format `plain_text` shape** (shown as the `plain_text` field only):

| Type | Example `plain_text` |
| --- | --- |
| docx (headers/footers + body + table) | `"Company header\nPage 1\nIntroduction\nname\tvalue\nalpha\t1"` |
| xlsx | `"Data\nname\tvalue\nalpha\t1\nbeta\t2"` |
| pptx (with speaker notes) | `"[Slide 1]\nSlide title\nSlide subtitle\nSpeaker notes here"` |
| pdf (multi-page) | `"[Page 1]\nHello pdf world\n[Page 2]\nSecond page body"` |
| txt / md | `"# Heading\n\nBody paragraph."` |

**legacy (`.doc` / `.xls` / `.ppt`)** — `file_type` reflects the original extension (e.g. `"doc"`); the text content is produced by converting the file to its OpenXML equivalent via LibreOffice first, so the shape of `plain_text` matches the corresponding OpenXML format.

### Plain-text formatting rules

The base class linearizes structured content into `plain_text` using these conventions:

- **Paragraphs** — joined with `\n`.
- **Tables** — rows joined with `\n`, cells joined with `\t`.
- **Sheets (xlsx)** — preceded by the sheet name on its own line, then rows as above.
- **Slides (pptx)** — preceded by `[Slide N]` on its own line.
- **Pages (pdf)** — preceded by `[Page N]` on its own line; page body is Markdown (headings as `#`, detected tables as pipe-tables, lists as `- `) because `pymupdf4llm` produces Markdown for better column-aware reading order.

### Request-level errors

Per-file failures live inside the response list (above). A 4xx only happens for malformed *requests* (e.g. missing the `files` field entirely):

```json
{ "detail": "No files provided" }
```

## Extraction behavior per type

- **docx/docm** — walks body *and* all section headers/footers in document order so paragraphs and tables interleave correctly. Table cells preserve multi-paragraph text (joined with `\n`).
- **xlsx/xlsm** — `openpyxl(data_only=True)` returns **evaluated formula results** rather than the formula string, matching what you see when you open the file in Excel. `None` cells become `""`.
- **pptx/pptm** — per slide, walks shapes in z-order, descends into groups, and extracts text frames + tables. Speaker notes are appended after slide body text.
- **legacy (`.doc` / `.xls` / `.ppt`)** — headless LibreOffice converts the file to its OpenXML equivalent in a temp dir, then the matching OpenXML extractor runs. A 60 s timeout is enforced; `soffice` stderr is surfaced on failure. Requires `soffice` on `PATH`. Each conversion uses an isolated `UserInstallation` profile so concurrent requests don't collide.
- **pdf** — `pymupdf4llm.to_markdown(..., page_chunks=True)` per page. Detects multi-column layouts and emits each column fully before moving to the next, matching natural reading order.
- **txt/md** — raw bytes with a four-step encoding fallback (`utf-8-sig` → `utf-8` → `utf-16` → `latin-1`).

## Concurrency

`/extract` is an `async` endpoint that streams the upload in 1 MiB chunks, then dispatches the synchronous extraction work to a threadpool via `run_in_threadpool`. A long soffice conversion on one request does not block other requests from being served.

## Configuration

All defaults live in [app/config.py](app/config.py):

| Constant | Default | Meaning |
| --- | --- | --- |
| `MAX_UPLOAD_MB` | `100` | Reject uploads larger than this with `413` |
| `LIBREOFFICE_TIMEOUT_SEC` | `60` | Kill the `soffice` subprocess after this many seconds |

## Tests

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -v
```

`tests/conftest.py` generates all fixtures on the fly, including the legacy `.doc/.xls/.ppt` files via `soffice` and a synthetic two-column PDF for the reading-order regression test. Tests that need `soffice` auto-skip when it is not on `PATH`.

## Project layout

```
app/
  main.py              # FastAPI app, /extract /health /supported
  config.py            # supported exts, limits
  schemas.py           # ExtractionResponse pydantic model
  dispatcher.py        # extension -> Extractor
  errors.py            # ExtractionError, UnsupportedFormatError, ConversionError
  extractors/
    base.py            # Extractor base class + elements_to_plain_text
    docx.py            # .docx / .docm
    xlsx.py            # .xlsx / .xlsm
    pptx.py            # .pptx / .pptm
    pdf.py             # .pdf
    text.py            # .txt / .md
    legacy.py          # .doc / .xls / .ppt (via soffice, delegates to OpenXML)
  services/
    libreoffice.py     # soffice subprocess wrapper
tests/
  conftest.py          # builds fixtures (incl. legacy via soffice)
  test_extract.py      # end-to-end tests for every supported extension
requirements.txt
Dockerfile
.dockerignore
```
