"""Detect whether a PDF page is a scanned image vs a real text page.

Cheap to call (sub-millisecond per page on PyMuPDF). Run on every page during
the first extractor sweep; only pages that fail this check are sent to OCR.
"""
from __future__ import annotations

from typing import Any

# A low-text page with at least this many vector drawing ops is treated as
# "text rendered as outlines" (fonts converted to curves) rather than a
# genuinely blank/decorative page. Such pages look full but yield almost no
# extractable text — the same content loss as a scan, with no raster image.
MIN_VECTOR_OPS = 100


def largest_image_ratio(page: Any) -> float:
    """Fraction of the page area covered by its single largest image (0..1).

    We use the largest single image, not the sum — a true scan is ONE raster
    that fills the page, whereas an authored page may carry several figures
    whose areas would sum misleadingly high.
    """
    page_area = float(page.rect.width) * float(page.rect.height)
    if page_area <= 0:
        return 0.0
    try:
        infos = page.get_image_info()
    except Exception:
        infos = []
    largest = 0.0
    for info in infos:
        bbox = info.get("bbox") if isinstance(info, dict) else None
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = bbox
        w = max(0.0, float(x1) - float(x0))
        h = max(0.0, float(y1) - float(y0))
        if w * h > largest:
            largest = w * h
    return largest / page_area


def has_scanned_image(page: Any, min_image_area_ratio: float) -> bool:
    """Return True if the page is a page-filling raster image — i.e. a scan —
    REGARDLESS of any text layer.

    This is the image-size signal used to LABEL a page as scanned. A scanned
    page can still carry a usable text layer (an embedded OCR layer, or just a
    footer watermark), so this is deliberately independent of text length.
    Whether that text is kept or skipped is decided separately by the caller.
    """
    return largest_image_ratio(page) >= min_image_area_ratio


def is_scanned_page(
    page: Any, min_chars_for_text: int, min_image_area_ratio: float
) -> bool:
    """Return True for a scanned page with NO usable text layer.

    Two-gate test:
      1. If the page has at least `min_chars_for_text` non-whitespace characters,
         it has usable text — handled as text, not as an empty scan.
      2. Otherwise, it's a scan only if its largest single image covers at least
         `min_image_area_ratio` of the page.

    This is the trigger for OCR / reject / skip — i.e. a scan we cannot read.
    Page-filling scans that DO carry usable text are detected by
    `has_scanned_image` instead (and their text is kept).
    """
    text = page.get_text() or ""
    if len(text.strip()) >= min_chars_for_text:
        return False
    return has_scanned_image(page, min_image_area_ratio)


def is_vector_text_page(
    page: Any, min_chars_for_text: int, min_vector_ops: int = MIN_VECTOR_OPS
) -> bool:
    """Return True if a low-text page is dense with vector drawings.

    This catches PDFs whose body text was rendered as vector outlines (fonts
    converted to curves) instead of a real text layer: `get_text()` yields
    almost nothing, yet there's no raster image either, so `is_scanned_page`
    misses it. The page is visually full but functionally unextractable.

    Genuinely blank or lightly-decorated pages have few drawings and are NOT
    flagged, so only real content loss is reported.

    Note: `get_drawings()` parses every vector path, so this is only worth
    calling on pages that already failed the text-layer check.
    """
    text = page.get_text() or ""
    if len(text.strip()) >= min_chars_for_text:
        return False
    try:
        return len(page.get_drawings()) >= min_vector_ops
    except Exception:
        return False
