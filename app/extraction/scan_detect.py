"""Detect whether a PDF page is a scanned image vs a real text page.

Cheap to call (sub-millisecond per page on PyMuPDF). Run on every page during
the first extractor sweep; only pages that fail this check are sent to OCR.
"""
from __future__ import annotations

from typing import Any


def is_scanned_page(
    page: Any, min_chars_for_text: int, min_image_area_ratio: float
) -> bool:
    """Return True if the page looks like a scanned image (no real text layer).

    Two-gate test:
      1. If the page has at least `min_chars_for_text` non-whitespace characters,
         it has a usable text layer — not scanned.
      2. Otherwise inspect images. A true scanned page is ONE raster that fills
         the page; we require the largest single image (not the sum) to cover
         at least `min_image_area_ratio` of the page. Summing areas would
         misclassify authored pages with multiple figures/photos as scans and
         waste OCR cycles on garbage extraction.
    """
    text = page.get_text() or ""
    if len(text.strip()) >= min_chars_for_text:
        return False

    page_area = float(page.rect.width) * float(page.rect.height)
    if page_area <= 0:
        return False

    try:
        infos = page.get_image_info()
    except Exception:
        infos = []
    if not infos:
        return False

    largest_image_area = 0.0
    for info in infos:
        bbox = info.get("bbox") if isinstance(info, dict) else None
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = bbox
        w = max(0.0, float(x1) - float(x0))
        h = max(0.0, float(y1) - float(y0))
        area = w * h
        if area > largest_image_area:
            largest_image_area = area

    return (largest_image_area / page_area) >= min_image_area_ratio
