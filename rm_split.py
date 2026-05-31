#!/usr/bin/env python3
"""
rm_split.py — split a tall reMarkable PDF into readable pages, in place.

Vendored from the standalone splitter (github.com/delize/remarkable-pdf-splitter,
`pdf_splitter.py`) so rm-ocr can do split -> OCR in a single pass instead of
coordinating with a second async tool. The algorithm (whitespace-band detection,
greedy ~target-height segmentation, /RemarkableSplitter metadata marker) is kept
faithful to upstream; only the packaging changed: pure functions, config passed in
as a dataclass, no module-level env reads or logging side effects.

System dependency: poppler's `pdftoppm` (already required by pdf2image). Pillow +
numpy are Python deps. Ghostscript is NOT required — compression lives in the
standalone tool and is intentionally omitted here (rm-ocr downscales to MAX_PX at
OCR time anyway).
"""
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass

log = logging.getLogger("rm-ocr")

# Same marker the standalone splitter writes, so the two are interchangeable.
SPLIT_MARKER_KEY = "/RemarkableSplitter"
SPLIT_MARKER_VALUE = "processed"


@dataclass
class SplitConfig:
    min_aspect_ratio: float = 2.0      # split a page only if height/width exceeds this
    target_page_height: int = 700      # desired output page height, px @ 72dpi
    min_gap_height: int = 25           # smallest whitespace band (px) worth cutting at
    whitespace_threshold: int = 248    # row mean-brightness (0-255) counted as "white"


def find_best_splits(image_path, target_page_height=700, min_gap=25,
                     whitespace_threshold=248):
    """Find y-coordinates to cut at, targeting ~equal pages that break on whitespace.

    Returns (list_of_y_split_points, total_image_height). Faithful port of the
    upstream greedy look-ahead algorithm.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(image_path).convert("L")
    arr = np.array(img)
    height, _ = arr.shape

    row_means = np.mean(arr, axis=1)
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


def is_already_split(pdf_path):
    """True if the PDF carries our metadata marker (already processed)."""
    from pypdf import PdfReader
    try:
        md = PdfReader(str(pdf_path)).metadata
        return bool(md and md.get(SPLIT_MARKER_KEY) == SPLIT_MARKER_VALUE)
    except Exception:
        return False


def max_aspect_ratio(pdf_path):
    """Tallest page's height/width, or 0.0 on read failure."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(pdf_path))
        mx = 0.0
        for page in reader.pages:
            w, h = float(page.mediabox.width), float(page.mediabox.height)
            if w:
                mx = max(mx, h / w)
        return mx
    except Exception as e:
        log.error("aspect check failed for %s: %s", pdf_path, e)
        return 0.0


def should_split(pdf_path, cfg):
    """True if any page is tall enough to need splitting."""
    return max_aspect_ratio(pdf_path) > cfg.min_aspect_ratio


def split_pdf(input_path, output_path, cfg):
    """Split a tall PDF into readable pages; write to output_path with the marker.

    Returns True on success. Pages that don't exceed the aspect ratio are passed
    through untouched. Faithful port of upstream split_pdf (minus compression).
    """
    from PIL import Image
    from pypdf import PdfReader, PdfWriter

    try:
        reader = PdfReader(str(input_path))
        writer = PdfWriter()
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                ["pdftoppm", "-png", "-r", "72", str(input_path), f"{tmpdir}/page"],
                check=True, capture_output=True,
            )
            total = 0
            for page_num, page in enumerate(reader.pages):
                img_path = f"{tmpdir}/page-{page_num + 1}.png"
                pdf_width = float(page.mediabox.width)
                pdf_height = float(page.mediabox.height)

                if not os.path.exists(img_path):
                    log.warning("no image for page %d, keeping original", page_num + 1)
                    writer.add_page(page)
                    total += 1
                    continue

                if pdf_width and pdf_height / pdf_width <= cfg.min_aspect_ratio:
                    writer.add_page(page)  # this page didn't need splitting
                    total += 1
                    continue

                img_height = Image.open(img_path).height
                scale = pdf_height / img_height
                splits, _ = find_best_splits(
                    img_path, cfg.target_page_height, cfg.min_gap_height, cfg.whitespace_threshold,
                )
                log.info("split: page %d %dpx -> %d cut(s)", page_num + 1, img_height, len(splits))

                if not splits:
                    writer.add_page(page)
                    total += 1
                    continue

                pdf_splits = sorted([pdf_height - (y * scale) for y in splits], reverse=True)
                boundaries = [pdf_height] + pdf_splits + [0]
                for i in range(len(boundaries) - 1):
                    top, bottom = boundaries[i], boundaries[i + 1]
                    if top - bottom < 30:  # skip slivers
                        continue
                    new_page = writer.add_blank_page(pdf_width, top - bottom)
                    new_page.merge_page(page)
                    new_page.mediabox.lower_left = (0, bottom)
                    new_page.mediabox.upper_right = (pdf_width, top)
                    total += 1

            writer.add_metadata({SPLIT_MARKER_KEY: SPLIT_MARKER_VALUE})
            writer.write(str(output_path))
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
