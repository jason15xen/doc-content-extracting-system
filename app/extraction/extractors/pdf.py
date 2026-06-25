from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import pymupdf

from app.errors import ExtractionError
from app.extraction.extractors.base import Extractor
from app.extraction.ocr import ocr_page
from app.extraction.scan_detect import (
    has_scanned_image,
    is_scanned_page,
    is_vector_text_page,
)

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
        skipped: list[int] = []
        scanned: list[int] = []
        ocr_eligible = self._ocr_pool is not None and self.file_type == "pdf"
        # Classifier-only mode targets PDFs (the only format that has
        # scanned content). For DOCX/PPTX the check is moot — scan_detect
        # always returns False on authored formats anyway.
        classify_only = self._reject_scanned and self.file_type == "pdf"
        # Detection runs for every PDF page regardless of OCR/reject config, so
        # scanned/unextractable pages can be labelled even when OCR is off.
        # Authored formats never look scanned/outlined, so this is a no-op there.
        detect = self.file_type == "pdf"
        total_pages = 0

        with pymupdf.open(path) as doc:
            for i, page in enumerate(doc, start=1):
                total_pages = i
                text = page.get_text() or ""
                text_len = len(text.strip())
                # A page-filling raster means the page is a scan, regardless of
                # any text layer (which may be embedded OCR or just a footer
                # watermark). Label it as scanned either way.
                page_is_scan = detect and has_scanned_image(
                    page, self._ocr_min_image_area
                )
                if page_is_scan:
                    scanned.append(i)

                # Usable text layer — keep it (even on a scanned page; we never
                # discard real text). The scan label above still stands.
                if text_len >= self._ocr_min_chars:
                    elements[i] = {"type": "page", "index": i, "text": text}
                    continue

                # No usable text. A page-filling scan with no readable text is
                # the OCR / reject / skip case.
                if page_is_scan:
                    if classify_only:
                        # Short-circuit: one unreadable scan is enough to
                        # reject. No need to walk the rest of the file.
                        raise ExtractionError(
                            f"rejected: scanned page detected at index {i} "
                            f"(OCR_REJECT_SCANNED=true)"
                        )
                    if ocr_eligible:
                        scan_indices.append(i)
                    else:
                        skipped.append(i)
                    continue
                # Not a raster scan. If the page is dense with vector drawings
                # but yields no text, its content is rendered as outlines and is
                # just as unextractable as a scan — skip and note it too.
                if detect and is_vector_text_page(page, self._ocr_min_chars):
                    skipped.append(i)
                elif text_len > 0:
                    # Genuinely thin (not a scan, not outlined) — keep what
                    # little text we have.
                    elements[i] = {"type": "page", "index": i, "text": text}
                # else: blank/decorative page → drop silently.

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
                else:
                    # OCR produced no text for this scanned page — it's dropped,
                    # so record it as skipped (it's already in `scanned`).
                    skipped.append(idx)

        # Expose scanned pages (labelling) and skipped pages (dropped) so the
        # pipeline can note both on the task file. Sorted because OCR-empty
        # pages are appended above, out of page order.
        self.scanned_pages = scanned
        self.skipped_pages = sorted(skipped)

        return [elements[i] for i in sorted(elements)]


def _ocr_page_unpack(args: tuple[str, int, int, str]) -> str:
    """Pickle-friendly wrapper: ProcessPoolExecutor.map only passes one arg
    per task, so we tuple-pack and unpack here. Top-level for picklability."""
    return ocr_page(*args)
