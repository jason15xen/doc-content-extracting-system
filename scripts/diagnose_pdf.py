"""Pre-ingest extractability diagnostic for PDFs.

Predicts how the ingestion pipeline will treat a PDF *before* you upload it, so
you can spot files that would index "successfully" while capturing almost no
text. It reuses the real `is_scanned_page` logic and the live OCR thresholds
from settings, so its verdicts match actual ingestion behaviour.

Per page it reports:
  - text-layer chars : what PyMuPDF's get_text() actually yields (what gets
                       chunked/embedded). This is NOT what a human or an OCR
                       reader sees on the page.
  - largest-img %    : largest single image as a fraction of page area — the
                       signal `is_scanned_page` uses (a true scan is one big
                       raster covering the page).
  - drawings         : vector path count. High drawings + low text = body text
                       rendered as vector outlines (fonts converted to curves):
                       visually full, but unextractable without OCR.

Per-page verdicts:
  text       text layer >= OCR_MIN_CHARS_FOR_TEXT — extracts fine.
  SCANNED    low text + a page-filling raster — detected as a scan (skipped /
             OCR'd / rejected depending on config).
  VECTOR     low text + heavy vector drawings — the silent-loss case: indexes
             as success but loses the page's visible content. Needs OCR.
  thin/blank low text, no big image, few drawings — genuinely near-empty.

    python scripts/diagnose_pdf.py file.pdf
    python scripts/diagnose_pdf.py /path/to/corpus            # all *.pdf, recursive
    python scripts/diagnose_pdf.py a.pdf b.pdf --pages        # force per-page table
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymupdf

# Allow `python scripts/diagnose_pdf.py ...` from anywhere: running a file in
# scripts/ puts scripts/ on sys.path, not the project root, so `app` isn't
# importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.extraction.scan_detect import MIN_VECTOR_OPS  # noqa: E402
from app.settings import get_settings  # noqa: E402

# Kept in lockstep with ingestion's own vector-page detector.
_VECTOR_HINT = MIN_VECTOR_OPS
# A genuine text PDF page yields ~800-2,000 chars. Below this average, even
# the pages that clear the char threshold are too thin to be real text.
_LOW_AVG_CHARS = 300


def _largest_image_ratio(page) -> float:
    parea = float(page.rect.width) * float(page.rect.height)
    if parea <= 0:
        return 0.0
    largest = 0.0
    try:
        infos = page.get_image_info()
    except Exception:
        infos = []
    for info in infos:
        bbox = info.get("bbox") if isinstance(info, dict) else None
        if not bbox or len(bbox) != 4:
            continue
        w = max(0.0, float(bbox[2]) - float(bbox[0]))
        h = max(0.0, float(bbox[3]) - float(bbox[1]))
        largest = max(largest, w * h)
    return largest / parea


def _page_verdict(text_len: int, img_ratio: float, n_draw: int,
                  min_chars: int, min_img_ratio: float) -> str:
    # A page-filling raster means the page is scanned regardless of text.
    if img_ratio >= min_img_ratio:
        # text kept (embedded OCR / footer) vs no usable text (skipped/OCR'd)
        return "SCAN+text" if text_len >= min_chars else "SCANNED"
    if text_len >= min_chars:
        return "text"
    if n_draw >= _VECTOR_HINT:
        return "VECTOR"
    return "thin/blank"


def diagnose_one(path: Path, min_chars: int, min_img_ratio: float,
                 show_pages: bool) -> dict:
    """Diagnose a single PDF; print its report and return a summary dict."""
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:
        print(f"\n{path}\n  ERROR: cannot open ({exc})")
        return {"path": path, "error": str(exc)}

    counts = {"text": 0, "SCAN+text": 0, "SCANNED": 0, "VECTOR": 0, "thin/blank": 0}
    total_chars = 0
    scanned_pages: list[int] = []
    vector_pages: list[int] = []

    rows: list[tuple] = []
    try:
        for i, page in enumerate(doc, start=1):
            text_len = len((page.get_text() or "").strip())
            img_ratio = _largest_image_ratio(page)
            try:
                n_draw = len(page.get_drawings())
            except Exception:
                n_draw = 0
            verdict = _page_verdict(text_len, img_ratio, n_draw, min_chars, min_img_ratio)

            counts[verdict] += 1
            total_chars += text_len
            if verdict in ("SCANNED", "SCAN+text"):
                scanned_pages.append(i)
            elif verdict == "VECTOR":
                vector_pages.append(i)
            rows.append((i, text_len, img_ratio, n_draw, verdict))
        n_pages = doc.page_count
    finally:
        doc.close()

    print(f"\n{path}  ({n_pages} pages)")
    if show_pages:
        print(f"  {'pg':>3} {'chars':>6} {'largest-img':>11} {'draws':>6}  verdict")
        for i, text_len, img_ratio, n_draw, verdict in rows:
            print(f"  {i:>3} {text_len:>6} {img_ratio * 100:>10.1f}% "
                  f"{n_draw:>6}  {verdict}")

    avg = total_chars / n_pages if n_pages else 0
    print(f"  summary: {total_chars} text chars total (~{avg:.0f}/page) | "
          f"text={counts['text']} SCAN+text={counts['SCAN+text']} "
          f"SCANNED={counts['SCANNED']} VECTOR={counts['VECTOR']} "
          f"thin/blank={counts['thin/blank']}")

    # Document-level risk verdict. VECTOR pages alone are suspicious; combined
    # with a low average char count (thin "text" pages too) it's a clear
    # silent-loss case.
    vector_frac = counts["VECTOR"] / n_pages if n_pages else 0
    if counts["VECTOR"] and (avg < _LOW_AVG_CHARS or vector_frac >= 0.25):
        print("  >> RISK: body text appears to be VECTOR OUTLINES "
              f"(~{avg:.0f} chars/page, {counts['VECTOR']}/{n_pages} pages). "
              "Ingestion will report SUCCESS but capture little. OCR needed.")
    elif counts["SCANNED"]:
        print(f"  >> {counts['SCANNED']} scanned page(s) with NO text "
              f"{[p for p in scanned_pages]}: skipped+noted (OCR off), OCR'd "
              "(OCR on), or whole-file rejected (OCR_REJECT_SCANNED). "
              + (f"{counts['SCAN+text']} more scanned page(s) keep their text."
                 if counts["SCAN+text"] else ""))
    elif counts["SCAN+text"]:
        print(f"  >> {counts['SCAN+text']} scanned page(s) WITH a text layer "
              f"{scanned_pages}: text is kept and indexed; document flagged as "
              "containing scanned pages.")
    elif total_chars == 0:
        print("  >> would FAIL ingestion: empty extraction (no text on any page).")
    else:
        print("  >> looks extractable.")

    return {
        "path": path, "pages": n_pages, "total_chars": total_chars,
        "scanned": scanned_pages, "vector": vector_pages, "counts": counts,
    }


def _gather(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out.extend(sorted(path.rglob("*.pdf")))
        elif path.suffix.lower() == ".pdf":
            out.append(path)
        else:
            print(f"skip (not a pdf): {path}", file=sys.stderr)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", help="PDF files or directories")
    parser.add_argument("-p", "--pages", action="store_true",
                        help="show the per-page table (auto-on for a single file)")
    args = parser.parse_args()

    settings = get_settings()
    min_chars = settings.ocr_min_chars_for_text
    min_img_ratio = settings.ocr_min_image_area_ratio

    files = _gather(args.paths)
    if not files:
        print("no PDF files found", file=sys.stderr)
        return 1

    print(f"thresholds: OCR_MIN_CHARS_FOR_TEXT={min_chars} "
          f"OCR_MIN_IMAGE_AREA_RATIO={min_img_ratio}")
    show_pages = args.pages or len(files) == 1

    summaries = [
        diagnose_one(f, min_chars, min_img_ratio, show_pages) for f in files
    ]

    if len(files) > 1:
        flagged = [s for s in summaries
                   if s.get("scanned") or s.get("vector") or s.get("error")]
        print(f"\n{'=' * 60}\n{len(files)} files | {len(flagged)} flagged "
              "(scanned / vector-outline / unreadable)")
        for s in flagged:
            if s.get("error"):
                print(f"  ERROR  {s['path']}")
            else:
                tags = []
                if s["vector"]:
                    tags.append(f"vector={len(s['vector'])}")
                if s["scanned"]:
                    tags.append(f"scanned={len(s['scanned'])}")
                print(f"  {', '.join(tags):<24} {s['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
