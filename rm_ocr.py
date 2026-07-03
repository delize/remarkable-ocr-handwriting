#!/usr/bin/env python3
"""
rm_ocr.py — transcribe handwriting with a local Qwen3-VL via Ollama.

Accepts ANY of:
  - a single .pdf, .zip, .rmdoc, or .rm file
  - a directory containing any mix of the above (searched recursively)

Bundles (.zip / .rmdoc) and loose pages (.rm) are rendered to PDF via the
shared rm_render module (which shells out to `rmc`). PDFs are processed as-is.

Setup (macOS, Apple Silicon):
  brew install ollama poppler
  brew install --cask inkscape        # needed by rmc for .zip/.rmdoc/.rm inputs (not for plain .pdf)
  brew services start ollama
  ollama pull qwen3-vl:8b
  pip3 install -r requirements.txt    # pulls pdf2image + rmc

Examples:
  python3 rm_ocr.py ~/Downloads/Notes.pdf
  python3 rm_ocr.py ~/Downloads/notebooks                # mixed folder
  python3 rm_ocr.py ~/Downloads/Notebook.rmdoc --out ~/Downloads/ocr_out
  python3 rm_ocr.py <input> --render-cache /var/state/rendered   # share daemon's cache
"""
import argparse
import base64
import io
import json
import os
import pathlib
import sys
import tempfile
import urllib.request
from pdf2image import convert_from_path

import rm_render

PROMPT = (
    "Transcribe all handwritten text on this page exactly as written. "
    "Preserve line breaks and rough layout. Output only the transcription as "
    "plain markdown, no commentary. If a word is genuinely illegible, write "
    "[illegible] rather than guessing at it."
)
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/api/generate"


def ocr_pdf(pdf, model, dpi, max_px, cpu=False, timeout=1800, threads=None, no_think=False):
    results = []
    opts = {"temperature": 0}
    if cpu:
        opts["num_gpu"] = 0   # 0 layers on GPU == CPU-only (num_gpu = #layers, not #GPUs)
    if threads:
        opts["num_thread"] = threads   # override Ollama's under-detected count (cgroup "max" bug)
    pages = convert_from_path(str(pdf), dpi=dpi)
    for n, page in enumerate(pages, 1):
        w, h = page.size
        s = min(1.0, max_px / max(w, h))
        if s < 1.0:
            page = page.resize((int(w * s), int(h * s)))
        buf = io.BytesIO()
        page.save(buf, format="PNG")
        payload = {
            "model": model,
            "prompt": PROMPT,
            "images": [base64.b64encode(buf.getvalue()).decode()],
            "stream": True,        # stream tokens: live progress + no decode-phase timeout
            "keep_alive": "30m",   # keep the model resident across pages/docs (no reload)
            "options": opts,
        }
        if no_think:
            payload["think"] = False   # OCR wants a direct transcription, not a reasoning trace
        req = urllib.request.Request(
            OLLAMA_URL, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        print(f"    page {n}/{len(pages)} (prefill on CPU may take minutes)...", end="", flush=True)
        parts = []
        # `timeout` is the per-read socket timeout; the first read blocks through
        # the whole prefill, so it must be generous on CPU.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                if obj.get("error"):
                    raise RuntimeError(obj["error"])
                if obj.get("response"):
                    parts.append(obj["response"])
                if obj.get("done"):
                    break
        print(f" {len(''.join(parts))} chars", flush=True)
        results.append((n, "".join(parts).strip()))
    return results


def transcribe_pdf(pdf, out_md, *, model, dpi=150, max_px=1568, threads=None,
                   no_think=False, timeout=1800, cpu=False, title=None):
    """Transcribe a single PDF to a plain ``# title`` / ``## Page N`` markdown file.

    Reusable core extracted from ``main()`` (Phase 0). The daemon does NOT call
    this — it writes its own frontmatter+backlink markdown — but it keeps the CLI
    path and any other caller on one code path, and returns the per-page metadata
    the manifest wants.

    Returns a dict: ``{pages, chars_per_page, out_path}``.
    """
    pdf = pathlib.Path(pdf)
    out_md = pathlib.Path(out_md)
    title = title or pdf.stem
    pages = ocr_pdf(pdf, model, dpi, max_px, cpu=cpu, timeout=timeout,
                    threads=threads, no_think=no_think)
    lines = [f"# {title}\n"]
    for n, text in pages:
        lines.append(f"\n## Page {n}\n\n{text}\n")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines))
    return {
        "pages": len(pages),
        "chars_per_page": [len(text) for _, text in pages],
        "out_path": str(out_md),
    }


def _safe(name):
    s = "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip()
    return s or "untitled"


def gather(input_path, work, cache_dir=None):
    """Return list of (title, pdf_path) for everything to OCR under input_path.

    Dispatches through rm_render: PDFs pass through, bundles/.rm are rendered.
    Per-file render failures log to stderr and skip the file rather than
    aborting the batch.
    """
    p = input_path
    if p.is_file():
        if p.suffix.lower() not in rm_render.SUPPORTED_INPUT_SUFFIXES:
            return []
        sources = [p]
    elif p.is_dir():
        sources = list(rm_render.iter_inputs(p))
    else:
        return []

    out = []
    for src in sources:
        try:
            result = rm_render.render_to_pdf(
                src,
                cache_dir=cache_dir,
                workdir=work if cache_dir is None else None,
            )
        except Exception as e:
            print(f"  [skip] {src.name}: {e}", file=sys.stderr)
            continue
        out.append((result.title, result.pdf))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="A .pdf, .zip, .rmdoc, or .rm file, or a folder containing any mix of those")
    ap.add_argument("--out", default=None, help="Output dir (default: ./ocr_out)")
    ap.add_argument("--model", default="qwen3-vl:8b")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--max-px", type=int, default=1568)
    ap.add_argument("--cpu", action="store_true", help="Force CPU-only (num_gpu=0) — simulates the GPU-less NAS")
    ap.add_argument("--timeout", type=int, default=1800, help="Per-page timeout in seconds (covers slow CPU prefill)")
    ap.add_argument("--threads", type=int, default=None, help="Force CPU thread count (e.g. 14 on a 13600K; works around Ollama's cgroup under-detection)")
    ap.add_argument("--no-think", action="store_true", help="Disable thinking/reasoning trace (much faster on CPU for 'thinking' models like qwen3.5)")
    ap.add_argument("--render-cache", default=os.environ.get("RM_OCR_RENDER_CACHE"),
                    help="Persistent render cache dir (default: ephemeral temp). Point at the daemon's STATE/rendered to share it.")
    args = ap.parse_args()

    input_path = pathlib.Path(args.input).expanduser()
    if not input_path.exists():
        sys.exit(f"Input not found: {input_path}")
    out = pathlib.Path(args.out).expanduser() if args.out else pathlib.Path.cwd() / "ocr_out"
    out.mkdir(parents=True, exist_ok=True)
    cache_dir = pathlib.Path(args.render_cache).expanduser() if args.render_cache else None

    with tempfile.TemporaryDirectory() as tmp:
        items = gather(input_path, pathlib.Path(tmp), cache_dir=cache_dir)
        if not items:
            sys.exit(f"Nothing to OCR under {input_path} (no .pdf / .zip / .rmdoc / .rm found).")
        print(f"{len(items)} document(s). model={args.model} dpi={args.dpi}\nout: {out}\n")
        for title, pdf in items:
            title = _safe(title)
            print(f"[{title}] OCR...", flush=True)
            try:
                transcribe_pdf(
                    pdf, out / f"{title}.md",
                    model=args.model, dpi=args.dpi, max_px=args.max_px,
                    threads=args.threads, no_think=args.no_think,
                    timeout=args.timeout, cpu=args.cpu, title=title,
                )
                print(f"        -> {title}.md\n", flush=True)
            except Exception as e:
                print(f"        FAILED: {e}\n", flush=True)

    print(f"Done. Transcripts in {out}")


if __name__ == "__main__":
    main()
