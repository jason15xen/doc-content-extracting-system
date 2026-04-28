import asyncio
import logging
import os
import tempfile
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.errors import ConversionError, ExtractionError, UnsupportedFormatError
from app.extraction.config import MAX_UPLOAD_MB, SUPPORTED_EXTENSIONS
from app.extraction.dispatcher import get_extractor
from app.extraction.schemas import ExtractionResponse

router = APIRouter(tags=["extract"])
_LOG = logging.getLogger("app.extract")


@router.post(
    "/extract",
    response_model=list[ExtractionResponse],
    response_model_exclude_none=True,
)
async def extract(
    files: Annotated[list[UploadFile], File(description="Documents to extract text from")],
) -> list[ExtractionResponse]:
    if not files:
        raise HTTPException(status_code=422, detail="No files provided")
    results = await asyncio.gather(*(_process_one(f) for f in files))
    return [ExtractionResponse(**r) for r in results]


async def _process_one(upload: UploadFile) -> dict[str, Any]:
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
        _LOG.exception("unexpected extraction failure for %s", filename)
        return {"filename": filename, "error": f"Extraction failed: {exc}"}
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
