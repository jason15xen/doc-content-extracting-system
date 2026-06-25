import os
import shutil
from typing import Any

from app.extraction.extractors.base import Extractor
from app.extraction.extractors.pdf import PymupdfExtractor
from app.extraction.extractors.xlsx import XlsxExtractor
from app.extraction.services import libreoffice

LEGACY_TARGET = {
    ".doc": ("docx", PymupdfExtractor("docx")),
    ".xls": ("xlsx", XlsxExtractor()),
    ".ppt": ("pptx", PymupdfExtractor("pptx")),
}


class LegacyExtractor(Extractor):
    def __init__(self, ext: str) -> None:
        self.ext = ext.lower()
        # Initialise base state (file_type, skipped_pages, scanned_pages).
        super().__init__(file_type=self.ext.lstrip("."))
        target_format, delegate = LEGACY_TARGET[self.ext]
        self.target_format = target_format
        self.delegate = delegate

    def extract_elements(self, path: str) -> list[dict[str, Any]]:
        converted = libreoffice.convert(path, self.target_format)
        outdir = os.path.dirname(converted)
        try:
            return self.delegate.extract_elements(converted)
        finally:
            shutil.rmtree(outdir, ignore_errors=True)
