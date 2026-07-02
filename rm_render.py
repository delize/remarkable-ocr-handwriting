#!/usr/bin/env python3
"""
rm_render.py — turn any reMarkable-shaped input into a PDF ready for OCR.

Single dispatch point used by both the CLI (`rm_ocr.py`) and the daemon
(`ocr_daemon.py`). Accepts:

  * `.pdf`               — passthrough, no rendering
  * `.zip` / `.rmdoc`    — extract, render each `.rm` page via `rmc`, merge
  * `.rm`                — single-page bundle, one `rmc` call

The merge step uses `pdfunite` (poppler) which is already an image-level
system dep. `rmc` ships as a pip package; pin it in requirements.txt and the
image picks it up. `rmc`'s PDF export shells out to **Inkscape** to rasterize
its intermediate SVG, so Inkscape must also be present on the host/image (see
Dockerfile / README prerequisites) — it is the only real system dependency
`rmc` has for this path; it does not use or need a browser.

The daemon passes `cache_dir=STATE/"rendered"` so a re-extracted-but-byte-
identical bundle is a cache hit (the key is the source bytes' sha256, NOT
the rendered PDF — that way an `rmc` upgrade is not a false invalidation
signal). The CLI passes `workdir=<temp>` for ephemeral one-shot use, or
`--render-cache PATH` to share the daemon's cache.
"""
import hashlib
import json
import logging
import os
import pathlib
import shutil
import subprocess
import tempfile
import zipfile
from typing import NamedTuple

import rm_strokes

log = logging.getLogger("rm-ocr")

# Suffixes the daemon and CLI both understand. Bundles are structurally
# identical zips; `.rmdoc` is just the modern extension.
BUNDLE_SUFFIXES = {".zip", ".rmdoc"}
LOOSE_PAGE_SUFFIX = ".rm"
SUPPORTED_INPUT_SUFFIXES = {".pdf"} | BUNDLE_SUFFIXES | {LOOSE_PAGE_SUFFIX}


class RenderResult(NamedTuple):
    pdf: pathlib.Path          # absolute path to a PDF ready for OCR
    title: str                 # visibleName / stem / "untitled"
    rendered: bool             # False = .pdf passthrough; True = we produced it
    source_sha256: str         # hash of the ORIGINAL input bytes (cache key)
    page_regions: list = None  # per-page stroke region hints (rm_strokes), or
                               # None: .pdf passthrough (no stroke data exists),
                               # extraction wasn't requested, or it wasn't cached


def iter_inputs(root):
    """Walk `root` recursively, yielding every supported input file (sorted)."""
    root = pathlib.Path(root)
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_INPUT_SUFFIXES:
            yield p


def render_to_pdf(src, *, cache_dir=None, workdir=None, extract_regions=False):
    """Render `src` to a PDF suitable for OCR.

    `.pdf` is returned as-is. For `.zip`/`.rmdoc`/`.rm`, `cache_dir` OR
    `workdir` is required (so the returned path outlives this call). When
    both are given, `cache_dir` wins.

    `extract_regions=True` additionally parses each source `.rm` page's
    stroke geometry (rm_strokes) into `RenderResult.page_regions` — a rough,
    best-effort "probably a sketch, not text" hint per page. Only possible
    for `.rm`-family inputs (a `.pdf` never carries stroke data, so it's
    always `page_regions=None` regardless of this flag). A cache hit reuses
    the sidecar written alongside the cached PDF the first time it was
    rendered with this flag on; if that sidecar doesn't exist, `page_regions`
    is `None` rather than forcing a re-render.

    Raises `ValueError` for unsupported suffixes and for bundles that yield
    zero pages, so the caller can record a recognized error instead of a
    crash.
    """
    src = pathlib.Path(src)
    suffix = src.suffix.lower()
    if suffix not in SUPPORTED_INPUT_SUFFIXES:
        raise ValueError(f"unsupported input: {src} (suffix {suffix!r})")

    if suffix == ".pdf":
        return RenderResult(pdf=src, title=src.stem, rendered=False,
                            source_sha256=_sha256_file(src))

    sha = _sha256_file(src)
    title = _title_for(src)

    if cache_dir is not None:
        cache_dir = pathlib.Path(cache_dir)
        cached = _cache_path(cache_dir, sha)
        if cached.exists():
            log.info("render: cache hit %s -> %s", src.name, cached.name)
            page_regions = _read_regions_sidecar(cache_dir, sha) if extract_regions else None
            return RenderResult(pdf=cached, title=title, rendered=True,
                                source_sha256=sha, page_regions=page_regions)

    if cache_dir is None and workdir is None:
        raise ValueError("render_to_pdf requires cache_dir or workdir for non-PDF input")

    with tempfile.TemporaryDirectory(prefix="rm-render-") as scratch:
        scratch_path = pathlib.Path(scratch)
        if suffix in BUNDLE_SUFFIXES:
            rendered, page_regions = _render_zip_bundle(src, scratch_path, extract_regions)
        else:  # loose .rm
            rendered, page_regions = _render_loose_rm(src, scratch_path, extract_regions)
        if rendered is None or not rendered.exists():
            raise ValueError(f"no pages rendered from {src.name}")

        if cache_dir is not None:
            out = _cache_path(cache_dir, sha)
            out.parent.mkdir(parents=True, exist_ok=True)
            staging_dir = cache_dir / ".tmp"
            staging_dir.mkdir(parents=True, exist_ok=True)
            # Same filesystem as the final shard so os.replace is atomic.
            staging = staging_dir / f"{sha}.{os.getpid()}.pdf"
            shutil.copy2(rendered, staging)
            os.replace(staging, out)
            log.info("render: %s -> %s", src.name, out)
            if extract_regions:
                _write_regions_sidecar(cache_dir, sha, page_regions)
        else:
            workdir_path = pathlib.Path(workdir)
            workdir_path.mkdir(parents=True, exist_ok=True)
            out = workdir_path / f"{sha[:16]}.pdf"
            shutil.copy2(rendered, out)

    return RenderResult(pdf=out, title=title, rendered=True, source_sha256=sha,
                        page_regions=page_regions)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_path(cache_dir, sha):
    """Two-level shard: cache_dir/<sha[:2]>/<sha>.pdf."""
    return pathlib.Path(cache_dir) / sha[:2] / f"{sha}.pdf"


def _regions_sidecar_path(cache_dir, sha):
    """Stroke-region sidecar next to the cached PDF: cache_dir/<sha[:2]>/<sha>.regions.json."""
    return pathlib.Path(cache_dir) / sha[:2] / f"{sha}.regions.json"


def _read_regions_sidecar(cache_dir, sha):
    """Read the cached page_regions, or None if never written for this sha."""
    p = _regions_sidecar_path(cache_dir, sha)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _write_regions_sidecar(cache_dir, sha, page_regions):
    """Write page_regions next to the cached PDF (atomic temp+rename, same as the PDF)."""
    out = _regions_sidecar_path(cache_dir, sha)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = cache_dir / ".tmp"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging = staging_dir / f"{sha}.regions.{os.getpid()}.json"
    staging.write_text(json.dumps(page_regions))
    os.replace(staging, out)


def _title_for(src):
    """visibleName (bundle .metadata) → file stem → 'untitled'."""
    if src.suffix.lower() in BUNDLE_SUFFIXES:
        visible = _read_visible_name(src)
        if visible:
            return visible
    stem = src.stem.strip()
    return stem or "untitled"


def _read_visible_name(src):
    """Return the bundle's visibleName without extracting the whole archive.

    None on any failure (missing .metadata, malformed JSON, empty value).
    """
    try:
        with zipfile.ZipFile(src) as z:
            meta_names = [n for n in z.namelist() if n.endswith(".metadata")]
            if not meta_names:
                return None
            raw = z.read(meta_names[0]).decode("utf-8", errors="replace")
            name = (json.loads(raw).get("visibleName") or "").strip()
            return name or None
    except Exception:
        return None


def _render_zip_bundle(src, workdir, extract_regions=False):
    """Extract a `.zip`/`.rmdoc`, render each `.rm` page, return (merged PDF, page_regions)."""
    ex = workdir / "bundle"
    ex.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as z:
        z.extractall(ex)
    content = next(ex.glob("*.content"), None)
    page_dir = content.with_suffix("") if content else ex
    if not page_dir.is_dir():
        page_dir = next((d for d in ex.iterdir() if d.is_dir()), ex)
    pages = _page_order(content, page_dir) if content else sorted(page_dir.glob("*.rm"))
    if not pages:
        return None, None

    pdfs = []
    page_regions = [] if extract_regions else None
    for i, rm in enumerate(pages):
        out_pdf = ex / f"page_{i:03d}.pdf"
        _run_rmc(rm, out_pdf)
        pdfs.append(out_pdf)
        if extract_regions:
            page_regions.append(_page_regions_safe(rm))

    merged = ex / "merged.pdf"
    if len(pdfs) == 1:
        shutil.copy(pdfs[0], merged)
    else:
        _run_pdfunite(pdfs, merged)
    return merged, page_regions


def _render_loose_rm(src, workdir, extract_regions=False):
    out_pdf = workdir / (src.stem + ".pdf")
    _run_rmc(src, out_pdf)
    page_regions = [_page_regions_safe(src)] if extract_regions else None
    return out_pdf, page_regions


def _page_regions_safe(rm_path):
    """rm_strokes.page_regions(), tolerating a single bad page (never aborts the render)."""
    try:
        return rm_strokes.page_regions(rm_path)
    except Exception as e:
        log.warning("stroke-region parse failed for %s: %s", rm_path.name, e)
        return []


def _page_order(content_path, page_dir):
    """Order .rm pages by the bundle's .content JSON, falling back to sorted names."""
    rms = {p.stem: p for p in page_dir.glob("*.rm")}
    try:
        c = json.loads(content_path.read_text())
        ids = []
        if isinstance(c.get("cPages"), dict):
            ids = [pg.get("id") for pg in c["cPages"].get("pages", [])]
        elif isinstance(c.get("pages"), list):
            ids = c["pages"]
        ordered = [rms[i] for i in ids if i in rms]
        if ordered:
            return ordered
    except Exception:
        pass
    return [rms[k] for k in sorted(rms)]


def _run_rmc(rm_src, out_pdf):
    """Render one `.rm` to PDF via the `rmc` console script (needs Inkscape on PATH)."""
    cmd = ["rmc", str(rm_src), "-o", str(out_pdf)]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError as e:
        raise RuntimeError("rmc not installed (pip install rmc)") from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"rmc failed on {rm_src.name}: {stderr or e}") from e
    # rmc's PDF export shells out to Inkscape and swallows a missing-Inkscape
    # FileNotFoundError internally, exiting 0 with an empty output file rather
    # than raising — catch that here instead of letting it surface later as a
    # confusing pdf2image/poppler error on an empty PDF.
    if not out_pdf.exists() or out_pdf.stat().st_size == 0:
        raise RuntimeError(
            f"rmc produced an empty PDF for {rm_src.name} — Inkscape is likely "
            "missing (rmc's PDF export needs it on PATH; apt install inkscape / "
            "brew install --cask inkscape)"
        )


def _run_pdfunite(pdfs, out):
    try:
        subprocess.run(["pdfunite", *map(str, pdfs), str(out)],
                       check=True, capture_output=True)
    except FileNotFoundError as e:
        raise RuntimeError("pdfunite not installed (apt install poppler-utils)") from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"pdfunite failed: {stderr or e}") from e
