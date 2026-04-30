from typing import Any

import pymupdf

from app.extraction.extractors.base import Extractor


class PymupdfExtractor(Extractor):
    """PDF / DOCX / PPTX text extraction via PyMuPDF.

    PyMuPDF opens Office formats by rendering them to a paginated layout,
    so the same per-page `get_text()` call handles all three. XLSX is
    deliberately routed elsewhere — PyMuPDF clips text-only columns when
    rendering spreadsheets and silently loses cell content.
    """

    def extract_elements(self, path: str) -> list[dict[str, Any]]:
        elements: list[dict[str, Any]] = []
        with pymupdf.open(path) as doc:
            for i, page in enumerate(doc, start=1):
                text = page.get_text()
                if text and text.strip():
                    elements.append({"type": "page", "index": i, "text": text})
        return elements
