import os
import shutil
from typing import Any

from app.extraction.extractors.base import Extractor
from app.extraction.extractors.docx import DocxExtractor
from app.extraction.extractors.pptx import PptxExtractor
from app.extraction.extractors.xlsx import XlsxExtractor
from app.extraction.services import libreoffice

LEGACY_TARGET = {
    ".doc": ("docx", DocxExtractor()),
    ".xls": ("xlsx", XlsxExtractor()),
    ".ppt": ("pptx", PptxExtractor()),
}


class LegacyExtractor(Extractor):
    def __init__(self, ext: str) -> None:
        self.ext = ext.lower()
        target_format, delegate = LEGACY_TARGET[self.ext]
        self.target_format = target_format
        self.delegate = delegate
        self.file_type = self.ext.lstrip(".")

    def extract_elements(self, path: str) -> list[dict[str, Any]]:
        converted = libreoffice.convert(path, self.target_format)
        outdir = os.path.dirname(converted)
        try:
            return self.delegate.extract_elements(converted)
        finally:
            shutil.rmtree(outdir, ignore_errors=True)
