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
docker run --rm -p 8000:8000 doc-extract
```

### Local

You need Python 3.10+ and, if you want to handle `.doc/.xls/.ppt`, LibreOffice installed (`soffice` on `PATH`).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
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

Upload one file as `multipart/form-data` under field `file`. Response is JSON.

```bash
curl -F 'file=@/path/to/report.xlsx' http://localhost:8000/extract
```

**Status codes:**

| Code | Meaning |
| --- | --- |
| `200` | Extraction succeeded |
| `413` | File exceeds `MAX_UPLOAD_MB` (default 100 MB) |
| `415` | Unsupported extension |
| `422` | Extraction or LibreOffice conversion failed |

## Response type

Defined in [app/schemas.py](app/schemas.py):

```python
class ExtractionResponse(BaseModel):
    filename: str     # original uploaded filename
    file_type: str    # normalized ext without dot, e.g. "docx"
    plain_text: str   # full textual content in reading order
```

Equivalent TypeScript:

```ts
type ExtractionResponse = {
  filename: string;
  file_type: "docx" | "docm" | "xlsx" | "xlsm" | "pptx" | "pptm"
           | "doc"  | "xls"  | "ppt"  | "pdf"  | "txt"  | "md";
  plain_text: string;
};
```

### Example responses

**docx** (headers/footers + body + table)

```json
{
  "filename": "memo.docx",
  "file_type": "docx",
  "plain_text": "Company header\nPage 1\nIntroduction\nname\tvalue\nalpha\t1"
}
```

**xlsx**

```json
{
  "filename": "report.xlsx",
  "file_type": "xlsx",
  "plain_text": "Data\nname\tvalue\nalpha\t1\nbeta\t2"
}
```

**pptx** (with speaker notes)

```json
{
  "filename": "deck.pptx",
  "file_type": "pptx",
  "plain_text": "[Slide 1]\nSlide title\nSlide subtitle\nSpeaker notes here"
}
```

**pdf**

```json
{
  "filename": "guide.pdf",
  "file_type": "pdf",
  "plain_text": "[Page 1]\nHello pdf world\n[Page 2]\nSecond page body"
}
```

**txt / md**

```json
{
  "filename": "notes.md",
  "file_type": "md",
  "plain_text": "# Heading\n\nBody paragraph."
}
```

**legacy (`.doc` / `.xls` / `.ppt`)** — `file_type` reflects the original extension (e.g. `"doc"`); the text content is produced by converting the file to its OpenXML equivalent via LibreOffice first, so the shape of `plain_text` matches the corresponding OpenXML format.

### Plain-text formatting rules

The base class linearizes structured content into `plain_text` using these conventions:

- **Paragraphs** — joined with `\n`.
- **Tables** — rows joined with `\n`, cells joined with `\t`.
- **Sheets (xlsx)** — preceded by the sheet name on its own line, then rows as above.
- **Slides (pptx)** — preceded by `[Slide N]` on its own line.
- **Pages (pdf)** — preceded by `[Page N]` on its own line; page body is Markdown (headings as `#`, detected tables as pipe-tables, lists as `- `) because `pymupdf4llm` produces Markdown for better column-aware reading order.

### Error response

FastAPI returns JSON with a single `detail` string for 4xx errors:

```json
{ "detail": "Unsupported file extension: .zip" }
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
