"""Process-pool OCR for scanned PDF pages.

Tesseract is CPU-bound, so we run it in worker processes (not threads) to
escape the GIL and to isolate Tesseract's C state from the FastAPI process.
The pool is a process-wide singleton: every concurrent doc shares the same
N workers, keeping total CPU bounded regardless of `INGEST_CONCURRENCY`.

The worker function `ocr_page` must be importable at module level for
ProcessPoolExecutor to pickle it.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import time
from concurrent.futures import ProcessPoolExecutor

_LOG = logging.getLogger("app.ocr")

# Per-worker cache of the most recently opened PDF. A scanned doc with N pages
# triggers N OCR calls; without this each call would re-open the file. The
# cache is module-level inside each worker process — workers are long-lived,
# so the handle persists across calls until the path changes.
_cached_path: str | None = None
_cached_doc = None  # type: ignore[var-annotated]


def _ocr_worker_init() -> None:
    """Verify Tesseract is reachable. Raised here, the failure surfaces on the
    first OCR call rather than as a silent missing-binary later."""
    import pytesseract

    pytesseract.get_tesseract_version()


def ocr_page(pdf_path: str, page_index: int, dpi: int, languages: str) -> str:
    """Render `page_index` of `pdf_path` at `dpi` and OCR it.

    Returns the recognized text, or "" on per-page failure (logged). A single
    bad page must not break a 1000-page doc.
    """
    global _cached_path, _cached_doc
    try:
        import pymupdf
        import pytesseract
        from PIL import Image
    except Exception as exc:
        _LOG.error("ocr worker import failed: %s", exc)
        return ""

    try:
        if _cached_path != pdf_path:
            if _cached_doc is not None:
                try:
                    _cached_doc.close()
                except Exception:
                    pass
            _cached_doc = pymupdf.open(pdf_path)
            _cached_path = pdf_path

        t0 = time.perf_counter()
        page = _cached_doc.load_page(page_index)
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        render_ms = (time.perf_counter() - t0) * 1000.0

        # --psm 6: assume a single uniform block of text — much faster than
        # auto segmentation (--psm 1) and accurate enough for typical scans.
        # --oem 1: LSTM-only engine (skips legacy Tesseract), faster and more
        # accurate on modern fonts.
        t1 = time.perf_counter()
        text = pytesseract.image_to_string(
            img, lang=languages, config="--psm 6 --oem 1"
        )
        ocr_ms = (time.perf_counter() - t1) * 1000.0
        _LOG.info(
            "ocr pid=%d page=%d %dx%d render=%.0fms tesseract=%.0fms chars=%d",
            os.getpid(),
            page_index,
            pix.width,
            pix.height,
            render_ms,
            ocr_ms,
            len(text),
        )
        return text
    except Exception as exc:
        _LOG.warning(
            "ocr failed on %s page %d: %s", pdf_path, page_index, exc
        )
        return ""


def build_ocr_pool(workers: int) -> ProcessPoolExecutor:
    """Construct the process pool. Uses `spawn` to keep workers free of any
    state inherited from the FastAPI parent (asyncio loop, DB engines, HTTP
    clients) — workers only need Tesseract + PyMuPDF.
    """
    ctx = mp.get_context("spawn")
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=ctx,
        initializer=_ocr_worker_init,
    )
