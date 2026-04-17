from app.errors import UnsupportedFormatError
from app.extraction.config import LEGACY_EXTS, SUPPORTED_EXTENSIONS
from app.extraction.extractors.base import Extractor
from app.extraction.extractors.docx import DocxExtractor
from app.extraction.extractors.legacy import LegacyExtractor
from app.extraction.extractors.pdf import PdfExtractor
from app.extraction.extractors.pptx import PptxExtractor
from app.extraction.extractors.text import TextExtractor
from app.extraction.extractors.xlsx import XlsxExtractor


def get_extractor(ext: str) -> Extractor:
    ext = ext.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(f"Unsupported file extension: {ext}")

    file_type = ext.lstrip(".")

    if ext in (".docx", ".docm"):
        return DocxExtractor(file_type=file_type)
    if ext in (".xlsx", ".xlsm"):
        return XlsxExtractor(file_type=file_type)
    if ext in (".pptx", ".pptm"):
        return PptxExtractor(file_type=file_type)
    if ext in LEGACY_EXTS:
        return LegacyExtractor(ext)
    if ext == ".pdf":
        return PdfExtractor()
    if ext in (".txt", ".md"):
        return TextExtractor(file_type=file_type)

    raise UnsupportedFormatError(f"No extractor for: {ext}")
