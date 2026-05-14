from __future__ import annotations

from typing import TYPE_CHECKING

from app.errors import UnsupportedFormatError
from app.extraction.config import LEGACY_EXTS, SUPPORTED_EXTENSIONS
from app.extraction.extractors.base import Extractor
from app.extraction.extractors.legacy import LegacyExtractor
from app.extraction.extractors.pdf import PymupdfExtractor
from app.extraction.extractors.text import TextExtractor
from app.extraction.extractors.xlsx import XlsxExtractor

if TYPE_CHECKING:
    from app.pipeline.context import PipelineContext


PYMUPDF_EXTS = {".pdf", ".docx", ".docm", ".pptx", ".pptm"}


def get_extractor(ext: str, ctx: "PipelineContext | None" = None) -> Extractor:
    ext = ext.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(f"Unsupported file extension: {ext}")

    file_type = ext.lstrip(".")

    if ext in PYMUPDF_EXTS:
        if ctx is not None:
            s = ctx.settings
            return PymupdfExtractor(
                file_type=file_type,
                ocr_pool=ctx.ocr_pool,
                ocr_dpi=s.ocr_dpi,
                ocr_min_chars_for_text=s.ocr_min_chars_for_text,
                ocr_min_image_area_ratio=s.ocr_min_image_area_ratio,
                ocr_languages=s.ocr_languages,
                reject_scanned=s.ocr_reject_scanned,
            )
        return PymupdfExtractor(file_type=file_type)
    if ext in (".xlsx", ".xlsm"):
        return XlsxExtractor(file_type=file_type)
    if ext in LEGACY_EXTS:
        return LegacyExtractor(ext)
    if ext in (".txt", ".md"):
        return TextExtractor(file_type=file_type)

    raise UnsupportedFormatError(f"No extractor for: {ext}")
