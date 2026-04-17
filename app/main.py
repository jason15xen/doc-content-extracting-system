import os
import tempfile

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


@app.post("/extract", response_model=ExtractionResponse)
async def extract(file: UploadFile = File(...)) -> ExtractionResponse:
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported file extension: {ext or '(none)'}")

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = tmp.name
            total = 0
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_MB} MB limit")
                tmp.write(chunk)

        extractor = get_extractor(ext)
        result = await run_in_threadpool(extractor.extract, tmp_path, filename)
        return ExtractionResponse(**result)
    except HTTPException:
        raise
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except ConversionError as exc:
        raise HTTPException(status_code=422, detail=f"Conversion failed: {exc}") from exc
    except ExtractionError as exc:
        raise HTTPException(status_code=422, detail=f"Extraction failed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Extraction failed: {exc}") from exc
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
