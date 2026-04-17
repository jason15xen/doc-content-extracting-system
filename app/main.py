import asyncio
import os
import tempfile
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.config import MAX_UPLOAD_MB, SUPPORTED_EXTENSIONS
from app.dispatcher import get_extractor
from app.errors import ConversionError, ExtractionError, UnsupportedFormatError
from app.schemas import ExtractionResponse

app = FastAPI(title="Document Content Extraction API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/supported")
def supported() -> dict[str, list[str]]:
    return {"extensions": sorted(SUPPORTED_EXTENSIONS)}


@app.post(
    "/extract",
    response_model=list[ExtractionResponse],
    response_model_exclude_none=True,
)
async def extract(files: list[UploadFile] = File(...)) -> list[ExtractionResponse]:
    if not files:
        raise HTTPException(status_code=422, detail="No files provided")

    results = await asyncio.gather(*(_process_one(f) for f in files))
    return [ExtractionResponse(**r) for r in results]


async def _process_one(upload: UploadFile) -> dict[str, Any]:
    """Extract a single upload. Never raises — returns either a success dict
    {filename, file_type, plain_text} or an error dict {filename, error} so
    that one bad file in a batch doesn't sink the rest."""
    filename = upload.filename or ""
    ext = os.path.splitext(filename)[1].lower()

    if ext not in SUPPORTED_EXTENSIONS:
        return {
            "filename": filename,
            "error": f"Unsupported file extension: {ext or '(none)'}",
        }

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = tmp.name
            total = 0
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    return {
                        "filename": filename,
                        "error": f"File exceeds {MAX_UPLOAD_MB} MB limit",
                    }
                tmp.write(chunk)

        extractor = get_extractor(ext)
        return await run_in_threadpool(extractor.extract, tmp_path, filename)
    except UnsupportedFormatError as exc:
        return {"filename": filename, "error": str(exc)}
    except ConversionError as exc:
        return {"filename": filename, "error": f"Conversion failed: {exc}"}
    except ExtractionError as exc:
        return {"filename": filename, "error": f"Extraction failed: {exc}"}
    except Exception as exc:
        return {"filename": filename, "error": f"Extraction failed: {exc}"}
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
