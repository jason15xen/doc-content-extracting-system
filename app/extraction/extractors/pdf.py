from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import pymupdf

from app.errors import ExtractionError
from app.extraction.extractors.base import Extractor
from app.extraction.ocr import ocr_page
from app.extraction.scan_detect import is_scanned_page

_LOG = logging.getLogger("app.extract")


class PymupdfExtractor(Extractor):
    """PDF / DOCX / PPTX text extraction via PyMuPDF.

    PyMuPDF opens Office formats by rendering them to a paginated layout,
    so the same per-page `get_text()` call handles all three. XLSX is
    deliberately routed elsewhere — PyMuPDF clips text-only columns when
    rendering spreadsheets and silently loses cell content.

    For PDFs only: pages with no text layer but covered by a large image are
    sent to a Tesseract process pool. DOCX/PPTX skip OCR — they're authored
    formats, scanned content is a PDF-only concept.

    Classifier-only mode: when `reject_scanned=True`, the extractor still
    runs scan detection but FAILS the whole doc the moment it finds a
    scanned page, instead of OCR-ing it. This is for first-pass indexing
    of text-only docs when OCR is too slow to afford; scanned docs get
    deferred for a separate pass later.
    """

    def __init__(
        self,
        file_type: str | None = None,
        *,
        ocr_pool: ProcessPoolExecutor | None = None,
        ocr_dpi: int = 300,
        ocr_min_chars_for_text: int = 50,
        ocr_min_image_area_ratio: float = 0.5,
        ocr_languages: str = "eng",
        reject_scanned: bool = False,
    ) -> None:
        super().__init__(file_type=file_type)
        self._ocr_pool = ocr_pool
        self._ocr_dpi = ocr_dpi
        self._ocr_min_chars = ocr_min_chars_for_text
        self._ocr_min_image_area = ocr_min_image_area_ratio
        self._ocr_languages = ocr_languages
        self._reject_scanned = reject_scanned

    def extract_elements(self, path: str) -> list[dict[str, Any]]:
        elements: dict[int, dict[str, Any]] = {}
        scan_indices: list[int] = []
        ocr_eligible = self._ocr_pool is not None and self.file_type == "pdf"
        # Classifier-only mode targets PDFs (the only format that has
        # scanned content). For DOCX/PPTX the check is moot — scan_detect
        # always returns False on authored formats anyway.
        classify_only = self._reject_scanned and self.file_type == "pdf"
        total_pages = 0

        with pymupdf.open(path) as doc:
            for i, page in enumerate(doc, start=1):
                total_pages = i
                text = page.get_text() or ""
                text_len = len(text.strip())
                # Strong text layer — use it.
                if text_len >= self._ocr_min_chars:
                    elements[i] = {"type": "page", "index": i, "text": text}
                    continue
                # Thin or no text. Two outcomes:
                #   (a) page is image-covered → OCR (or reject if classifier-only)
                #   (b) page is blank/decorative → drop, OR keep the thin
                #       text if it's the only signal we have (better than
                #       nothing, mirrors today's behaviour for non-PDFs)
                if (ocr_eligible or classify_only) and is_scanned_page(
                    page, self._ocr_min_chars, self._ocr_min_image_area
                ):
                    if classify_only:
                        # Short-circuit: one scanned page is enough to
                        # reject. No need to walk the rest of the file.
                        raise ExtractionError(
                            f"rejected: scanned page detected at index {i} "
                            f"(OCR_REJECT_SCANNED=true)"
                        )
                    scan_indices.append(i)
                elif text_len > 0:
                    elements[i] = {"type": "page", "index": i, "text": text}

        if scan_indices and self._ocr_pool is not None:
            _LOG.info(
                "ocr: %d/%d pages scanned in %s",
                len(scan_indices), total_pages, path,
            )
            page_args = [
                (path, idx - 1, self._ocr_dpi, self._ocr_languages)
                for idx in scan_indices
            ]
            try:
                results = list(
                    self._ocr_pool.map(_ocr_page_unpack, page_args, chunksize=1)
                )
            except Exception:
                _LOG.exception("ocr pool failed for %s", path)
                results = ["" for _ in scan_indices]
            for idx, ocr_text in zip(scan_indices, results):
                if ocr_text and ocr_text.strip():
                    elements[idx] = {
                        "type": "page", "index": idx, "text": ocr_text,
                    }

        return [elements[i] for i in sorted(elements)]


def _ocr_page_unpack(args: tuple[str, int, int, str]) -> str:
    """Pickle-friendly wrapper: ProcessPoolExecutor.map only passes one arg
    per task, so we tuple-pack and unpack here. Top-level for picklability."""
    return ocr_page(*args)
