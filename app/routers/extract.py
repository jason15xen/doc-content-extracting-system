import asyncio
import logging
import os
import tempfile
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.deps import get_pipeline_context
from app.errors import ConversionError, ExtractionError, UnsupportedFormatError
from app.extraction.config import MAX_UPLOAD_MB, SUPPORTED_EXTENSIONS
from app.extraction.dispatcher import get_extractor
from app.extraction.extractors.di import (
    DI_SUPPORTED_EXTS,
    DocumentIntelligenceExtractor,
)
from app.extraction.schemas import ExtractionResponse
from app.pipeline.context import PipelineContext

router = APIRouter(tags=["extract"])
_LOG = logging.getLogger("app.extract")


@router.post(
    "/extract",
    response_model=list[ExtractionResponse],
    response_model_exclude_none=True,
)
async def extract(
    files: Annotated[list[UploadFile], File(description="Documents to extract text from")],
    ctx: PipelineContext = Depends(get_pipeline_context),
) -> list[ExtractionResponse]:
    if not files:
        raise HTTPException(status_code=422, detail="No files provided")
    results = await asyncio.gather(*(_process_one(f, ctx) for f in files))
    return [ExtractionResponse(**r) for r in results]


async def _process_one(upload: UploadFile, ctx: PipelineContext) -> dict[str, Any]:
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

        extractor = get_extractor(ext, ctx)
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


@router.post(
    "/extract-di",
    response_model=list[ExtractionResponse],
    response_model_exclude_none=True,
)
async def extract_di(
    files: Annotated[
        list[UploadFile],
        File(description="Documents to extract via Azure Document Intelligence"),
    ],
    ctx: PipelineContext = Depends(get_pipeline_context),
) -> list[ExtractionResponse]:
    """A/B-comparison endpoint: routes uploads through Azure Document
    Intelligence (`prebuilt-read`) instead of the local PyMuPDF + RapidOCR
    pipeline. Same response shape as `/extract` so timings can be compared
    directly via the request-timing middleware log."""
    if not files:
        raise HTTPException(status_code=422, detail="No files provided")
    if (
        not ctx.settings.azure_document_intelligence_endpoint
        or not ctx.settings.azure_document_intelligence_api_key
    ):
        raise HTTPException(
            status_code=503,
            detail="Azure Document Intelligence is not configured",
        )
    results = await asyncio.gather(*(_process_one_di(f, ctx) for f in files))
    return [ExtractionResponse(**r) for r in results]


async def _process_one_di(
    upload: UploadFile, ctx: PipelineContext
) -> dict[str, Any]:
    filename = upload.filename or ""
    ext = os.path.splitext(filename)[1].lower()

    if ext not in DI_SUPPORTED_EXTS:
        return {
            "filename": filename,
            "error": f"Document Intelligence does not support: {ext or '(none)'}",
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

        extractor = DocumentIntelligenceExtractor(
            file_type=ext.lstrip("."),
            endpoint=ctx.settings.azure_document_intelligence_endpoint,
            api_key=ctx.settings.azure_document_intelligence_api_key,
        )
        return await run_in_threadpool(extractor.extract, tmp_path, filename)
    except ExtractionError as exc:
        return {"filename": filename, "error": f"Extraction failed: {exc}"}
    except Exception as exc:
        _LOG.exception("unexpected DI extraction failure for %s", filename)
        return {"filename": filename, "error": f"Extraction failed: {exc}"}
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
