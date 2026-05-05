"""Process-pool OCR for scanned PDF pages.

RapidOCR (PP-OCRv4 ONNX models) is CPU-bound, so we run it in worker
processes to escape the GIL. The pool is a process-wide singleton: every
concurrent doc shares the same N workers, keeping total CPU bounded
regardless of `INGEST_CONCURRENCY`.

Each worker holds its own RapidOCR engine — model load happens once at
worker init, not per page.

The worker function `ocr_page` must be importable at module level for
ProcessPoolExecutor to pickle it.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_LOG = logging.getLogger("app.ocr")

# Per-worker cache of the most recently opened PDF. A scanned doc with N pages
# triggers N OCR calls; without this each call would re-open the file. The
# cache is module-level inside each worker process — workers are long-lived,
# so the handle persists across calls until the path changes.
_cached_path: str | None = None
_cached_doc = None  # type: ignore[var-annotated]
_ocr_engine = None  # type: ignore[var-annotated]


def _pin_onnxruntime_threads() -> None:
    """Force every onnxruntime.InferenceSession created in this worker to
    use a single intra-op / inter-op thread.

    The RapidOCR-level kwargs (det_intra_op_num_threads=…) are not honored
    by all versions of `rapidocr-onnxruntime`, and the OMP/BLAS env vars
    don't affect ONNX Runtime's own intraop threadpool. The only reliable
    pin is on `SessionOptions` itself, so we wrap `InferenceSession.__init__`
    to inject the limits regardless of how the caller configures it.

    Without this, N worker processes × ~all-cores intraop threads each
    thrash the CPU and per-page OCR runs 30-60s instead of 1-3s.
    """
    import onnxruntime as ort

    _original_init = ort.InferenceSession.__init__

    def _patched_init(
        self,
        path_or_bytes,
        sess_options=None,
        providers=None,
        provider_options=None,
        **kwargs,
    ):
        if sess_options is None:
            sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = 1
        sess_options.inter_op_num_threads = 1
        return _original_init(
            self,
            path_or_bytes,
            sess_options=sess_options,
            providers=providers,
            provider_options=provider_options,
            **kwargs,
        )

    ort.InferenceSession.__init__ = _patched_init


def _create_ocr_engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def _ocr_worker_init(logs_dir_str: str) -> None:
    """Pre-load the RapidOCR ONNX models. Without this, the first OCR call
    per worker pays a model-load tax (~1-2s) that compounds on a cold pool.

    Also: pin ONNX threads, cap BLAS/OpenMP, and re-attach the parent's
    file-logging handler so per-page timing logs reach the app log file
    (spawn workers don't inherit logging handlers from the parent).
    """
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

    _pin_onnxruntime_threads()

    try:
        from app.services.logging_setup import setup_file_logging

        setup_file_logging(Path(logs_dir_str))
    except Exception:
        # Log file isn't reachable from the worker — keep OCR running, the
        # main process still gets the dispatcher-level summary logs.
        pass

    global _ocr_engine
    _ocr_engine = _create_ocr_engine()


def ocr_page(pdf_path: str, page_index: int, dpi: int, languages: str) -> str:
    """Render `page_index` of `pdf_path` at `dpi` and OCR it.

    Returns the recognized text, or "" on per-page failure (logged). A single
    bad page must not break a 1000-page doc. `languages` is accepted for
    compatibility with the dispatcher signature; RapidOCR's default models
    cover Latin scripts and digits without per-call tuning.
    """
    global _cached_path, _cached_doc, _ocr_engine
    try:
        import numpy as np
        import pymupdf
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
        # numpy view of the raw RGB samples — RapidOCR accepts arrays directly,
        # so we skip the PIL round-trip the Tesseract path required.
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, 3
        )
        render_ms = (time.perf_counter() - t0) * 1000.0

        if _ocr_engine is None:
            _ocr_engine = _create_ocr_engine()

        t1 = time.perf_counter()
        # use_cls=False skips the orientation classifier — pages here are
        # always upright, and Cls runs once per detected line, so dropping
        # it cuts inference count by ~1/3 on dense pages with no recall loss.
        result, _elapse = _ocr_engine(img, use_cls=False)
        ocr_ms = (time.perf_counter() - t1) * 1000.0

        # Result is a list of [bbox, text, score] in detector reading order.
        if result:
            text = "\n".join(
                line[1] for line in result if line and len(line) >= 2
            )
        else:
            text = ""

        _LOG.info(
            "ocr pid=%d page=%d %dx%d render=%.0fms ocr=%.0fms chars=%d",
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


def build_ocr_pool(workers: int, logs_dir: Path) -> ProcessPoolExecutor:
    """Construct the process pool. Uses `spawn` to keep workers free of any
    state inherited from the FastAPI parent (asyncio loop, DB engines, HTTP
    clients) — workers only need RapidOCR + PyMuPDF.

    `logs_dir` is forwarded to each worker so it can attach the same daily
    file handler the parent uses; without it, per-page logs vanish.
    """
    ctx = mp.get_context("spawn")
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=ctx,
        initializer=_ocr_worker_init,
        initargs=(str(logs_dir),),
    )
