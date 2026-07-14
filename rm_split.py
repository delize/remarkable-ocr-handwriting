#!/usr/bin/env python3
"""
rm_split.py — split a tall reMarkable PDF into readable pages, in place.

Shares its algorithm with the standalone splitter (github.com/delize/
remarkable-pdf-splitter): whitespace-band detection, greedy ~target-height
segmentation, and the /RemarkableSplitter metadata marker, so the two tools
stay interchangeable.

Assembly and rendering run on PyMuPDF (fitz) rather than pypdf + pdftoppm.
The original pypdf port assembled each output page with merge_page(), which
re-encodes the ENTIRE source content stream once per segment in pure Python.
On raster (Scrybble-style) exports that is merely wasteful, but on native
vector exports (the ink IS the content stream) it goes quadratic: a real
continuous-page notebook took 25+ minutes and OOM'd a 2 GB container, where
PyMuPDF finishes in seconds. show_pdf_page() embeds the source page once as a
form XObject and each output page just references it with a clip rect, so the
output file stays roughly input-sized. Rendering for the whitespace analysis
happens in-process per page (only for pages that are actually tall) instead
of shelling out to pdftoppm for the whole document up front.

New over the original algorithm: a forced-cut fallback. When a stretch of
content has no detectable whitespace band (dark templates, dense sketches),
the greedy pass used to give up and emit the stretch uncut — the worst case
being a 60-inch page passed through whole. Segments longer than
target_page_height * max_segment_factor are now subdivided evenly instead
(set max_segment_factor=0 to restore the old behavior).

Python deps: PyMuPDF + numpy, both lazily imported. No poppler, no
Ghostscript — compression lives in the standalone tool and is intentionally
omitted here (rm-ocr downscales to MAX_PX at OCR time anyway).
"""
import logging
import math
import os
from dataclasses import dataclass

log = logging.getLogger("rm-ocr")

# Same marker the standalone splitter writes, so the two are interchangeable.
# The leading slash is PDF-syntax for Info-dictionary keys (what pypdf shows);
# fitz addresses the same key without it.
SPLIT_MARKER_KEY = "/RemarkableSplitter"
SPLIT_MARKER_VALUE = "processed"


@dataclass
class SplitConfig:
    min_aspect_ratio: float = 2.0      # split a page only if height/width exceeds this
    target_page_height: int = 700      # desired output page height, px @ 72dpi
    min_gap_height: int = 25           # smallest whitespace band (px) worth cutting at
    whitespace_threshold: int = 248    # row mean-brightness (0-255) counted as "white"
    max_segment_factor: float = 2.0    # force-cut segments taller than target * this (0 = never)


def find_best_splits(gray_rows, target_page_height=700, min_gap=25,
                     whitespace_threshold=248):
    """Find y-coordinates to cut at, targeting ~equal pages that break on whitespace.

    gray_rows is a 2D uint8 numpy array (grayscale page raster, row-major).
    Returns (list_of_y_split_points, total_image_height). Same greedy
    look-ahead algorithm as the standalone splitter.
    """
    import numpy as np

    height = gray_rows.shape[0]
    row_means = np.mean(gray_rows, axis=1)
    is_white_row = row_means > whitespace_threshold

    potential_splits = []
    in_gap = False
    gap_start = 0
    for y in range(height):
        if is_white_row[y] and not in_gap:
            in_gap = True
            gap_start = y
        elif not is_white_row[y] and in_gap:
            gap_height = y - gap_start
            if gap_height >= min_gap:
                potential_splits.append({"y": gap_start + gap_height // 2, "gap_size": gap_height})
            in_gap = False

    if not potential_splits:
        return [], height

    selected_splits = []
    last_split = 0
    for split in potential_splits:
        if split["y"] - last_split >= target_page_height * 0.7:
            better_option = None
            for future in potential_splits:
                if future["y"] > split["y"] and future["y"] - last_split <= target_page_height * 1.3:
                    if future["gap_size"] > split["gap_size"] * 1.3:
                        better_option = future
                        break
            chosen = better_option if better_option else split
            selected_splits.append(chosen["y"])
            last_split = chosen["y"]

    return selected_splits, height


def subdivide_long_segments(splits, height, target_page_height, max_segment_factor):
    """Force cuts through segments the whitespace pass left too tall.

    Whitespace detection finds nothing to cut on dark templates or dense ink,
    which used to pass a 60-inch page through whole. Any segment taller than
    target_page_height * max_segment_factor gets subdivided into equal parts
    no taller than target_page_height. Returns a new sorted split list.
    """
    if max_segment_factor <= 0:
        return splits
    limit = target_page_height * max_segment_factor
    out = []
    boundaries = [0] + sorted(splits) + [height]
    for top, bottom in zip(boundaries, boundaries[1:]):
        seg = bottom - top
        if seg > limit:
            pieces = math.ceil(seg / target_page_height)
            step = seg / pieces
            out.extend(round(top + step * i) for i in range(1, pieces))
        if bottom != height:
            out.append(bottom)
    return sorted(set(out))


def _info_xref(doc):
    """xref number of the PDF's Info dictionary, or 0 if there isn't one."""
    key_type, value = doc.xref_get_key(-1, "Info")
    if key_type != "xref":
        return 0
    return int(value.split()[0])


def is_already_split(pdf_path):
    """True if the PDF carries our metadata marker (already processed)."""
    import fitz
    try:
        with fitz.open(str(pdf_path)) as doc:
            xref = _info_xref(doc)
            if not xref:
                return False
            key_type, value = doc.xref_get_key(xref, SPLIT_MARKER_KEY.lstrip("/"))
            return key_type == "string" and value == SPLIT_MARKER_VALUE
    except Exception:
        return False


def max_aspect_ratio(pdf_path):
    """Tallest page's height/width, or 0.0 on read failure."""
    import fitz
    try:
        with fitz.open(str(pdf_path)) as doc:
            mx = 0.0
            for page in doc:
                r = page.rect
                if r.width:
                    mx = max(mx, r.height / r.width)
            return mx
    except Exception as e:
        log.error("aspect check failed for %s: %s", pdf_path, e)
        return 0.0


def should_split(pdf_path, cfg):
    """True if any page is tall enough to need splitting."""
    return max_aspect_ratio(pdf_path) > cfg.min_aspect_ratio


def _page_gray(page):
    """Render a page to a 2D uint8 grayscale array at 72 dpi (1 px = 1 pt)."""
    import fitz
    import numpy as np
    pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), colorspace=fitz.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    # pix.stride can exceed width (row padding); slice it off after reshaping.
    return arr.reshape(pix.height, pix.stride)[:, : pix.width]


def split_pdf(input_path, output_path, cfg):
    """Split a tall PDF into readable pages; write to output_path with the marker.

    Returns True on success. Pages that don't exceed the aspect ratio are
    passed through untouched (annotations and links included). Split pages
    reference the source page's content once via a form XObject, so the
    output stays roughly input-sized no matter how many cuts are made.
    """
    import fitz

    try:
        src = fitz.open(str(input_path))
        out = fitz.open()
        total = 0
        for page_num in range(len(src)):
            page = src[page_num]
            pdf_width, pdf_height = page.rect.width, page.rect.height

            if not pdf_width or pdf_height / pdf_width <= cfg.min_aspect_ratio:
                out.insert_pdf(src, from_page=page_num, to_page=page_num)
                total += 1
                continue

            gray = _page_gray(page)
            img_height = gray.shape[0]
            scale = pdf_height / img_height  # ~1.0 at 72 dpi, kept for exactness
            splits, _ = find_best_splits(
                gray, cfg.target_page_height, cfg.min_gap_height, cfg.whitespace_threshold,
            )
            splits = subdivide_long_segments(
                splits, img_height, cfg.target_page_height, cfg.max_segment_factor,
            )
            log.info("split: page %d %dpx -> %d cut(s)", page_num + 1, img_height, len(splits))

            if not splits:
                out.insert_pdf(src, from_page=page_num, to_page=page_num)
                total += 1
                continue

            boundaries = [0.0] + [y * scale for y in splits] + [pdf_height]
            for top, bottom in zip(boundaries, boundaries[1:]):
                if bottom - top < 30:  # skip slivers
                    continue
                seg = out.new_page(width=pdf_width, height=bottom - top)
                seg.show_pdf_page(seg.rect, src, page_num,
                                  clip=fitz.Rect(0, top, pdf_width, bottom))
                total += 1

        # Stamp the marker into the Info dictionary. set_metadata() first, so
        # the Info object exists (fitz only writes standard keys itself).
        out.set_metadata({k: v for k, v in (src.metadata or {}).items()
                          if isinstance(v, str)})
        out.xref_set_key(_info_xref(out), SPLIT_MARKER_KEY.lstrip("/"),
                         fitz.get_pdf_str(SPLIT_MARKER_VALUE))
        out.save(str(output_path), deflate=True, garbage=3)
        out.close()
        src.close()
        log.info("split: wrote %s (%d pages)", output_path, total)
        return True
    except Exception as e:
        log.error("split failed for %s: %s", input_path, e)
        return False


def split_in_place(pdf_path, cfg):
    """Split pdf_path and replace it atomically. Returns True if the file was rewritten.

    No-op (returns False) if it's already split or doesn't need splitting. Writes to
    a temp file in the same directory first, then os.replace — so a crash mid-split
    never leaves a truncated PDF where the source was.
    """
    import pathlib
    pdf_path = pathlib.Path(pdf_path)
    if is_already_split(pdf_path):
        return False
    if not should_split(pdf_path, cfg):
        return False
    tmp = pdf_path.with_suffix(pdf_path.suffix + ".rmsplit.tmp")
    try:
        if not split_pdf(pdf_path, tmp, cfg):
            return False
        os.replace(tmp, pdf_path)  # atomic same-filesystem rename
        return True
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
