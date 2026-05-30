#!/usr/bin/env python3
"""
rm_ocr.py — transcribe handwriting with a local Qwen3-VL via Ollama.

Accepts ANY of:
  - a single .pdf file
  - a directory of .pdf files (searched recursively)        <- Obsidian vault case
  - a directory of reMarkable .zip bundles (rendered via rmc first)

Setup (macOS, Apple Silicon):
  brew install ollama poppler          # + `brew install cairo` only if rendering bundles
  brew services start ollama
  ollama pull qwen3-vl:8b
  pip3 install pdf2image
  pipx install rmc                     # only needed for .zip bundles

Examples:
  python3 rm_ocr.py "~/Vault/remarkable/Work/Carol.pdf"
  python3 rm_ocr.py "~/Vault/remarkable/Notes"
  python3 rm_ocr.py ~/Downloads/remarkable               # folder of .zip bundles
  python3 rm_ocr.py <input> --out ~/Downloads/ocr_out --dpi 300
"""
import argparse
import base64
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pdf2image import convert_from_path

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


# ---- reMarkable .zip bundle rendering (only used if no PDFs are found) ----

def _page_order(content_path, page_dir):
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


def _render_bundle(zip_path, workdir, use_chrome):
    ex = workdir / zip_path.stem
    ex.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(ex)
    content = next(ex.glob("*.content"), None)
    meta = next(ex.glob("*.metadata"), None)
    title = zip_path.stem
    if meta:
        try:
            title = json.loads(meta.read_text()).get("visibleName") or title
        except Exception:
            pass
    page_dir = content.with_suffix("") if content else ex
    if not page_dir.is_dir():
        page_dir = next((d for d in ex.iterdir() if d.is_dir()), ex)
    pages = _page_order(content, page_dir) if content else sorted(page_dir.glob("*.rm"))
    if not pages:
        return title, None
    pdfs = []
    for i, rm in enumerate(pages):
        out_pdf = ex / f"page_{i:03d}.pdf"
        cmd = ["rmc", str(rm), "-o", str(out_pdf)]
        if not use_chrome:
            cmd.insert(1, "--no-chrome")
        subprocess.run(cmd, check=True, capture_output=True)
        pdfs.append(out_pdf)
    merged = ex / "merged.pdf"
    if len(pdfs) == 1:
        shutil.copy(pdfs[0], merged)
    else:
        subprocess.run(["pdfunite", *map(str, pdfs), str(merged)], check=True, capture_output=True)
    return title, merged


def _safe(name):
    s = "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip()
    return s or "untitled"


def gather(input_path, work, use_chrome):
    """Return list of (title, pdf_path) to OCR."""
    p = input_path
    if p.is_file() and p.suffix.lower() == ".pdf":
        return [(p.stem, p)]
    if p.is_dir():
        pdfs = sorted(p.rglob("*.pdf"))
        if pdfs:
            return [(pdf.stem, pdf) for pdf in pdfs]
        zips = sorted(p.glob("*.zip"))
        if zips:
            out = []
            for z in zips:
                title, merged = _render_bundle(z, work, use_chrome)
                if merged:
                    out.append((_safe(title), merged))
            return out
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="A .pdf file, a folder of PDFs, or a folder of .zip bundles")
    ap.add_argument("--out", default=None, help="Output dir (default: ./ocr_out)")
    ap.add_argument("--model", default="qwen3-vl:8b")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--max-px", type=int, default=1568)
    ap.add_argument("--chrome", action="store_true", help="Use Chrome for rmc bundle rendering")
    ap.add_argument("--cpu", action="store_true", help="Force CPU-only (num_gpu=0) — simulates the GPU-less NAS")
    ap.add_argument("--timeout", type=int, default=1800, help="Per-page timeout in seconds (covers slow CPU prefill)")
    ap.add_argument("--threads", type=int, default=None, help="Force CPU thread count (e.g. 14 on a 13600K; works around Ollama's cgroup under-detection)")
    ap.add_argument("--no-think", action="store_true", help="Disable thinking/reasoning trace (much faster on CPU for 'thinking' models like qwen3.5)")
    args = ap.parse_args()

    input_path = pathlib.Path(args.input).expanduser()
    if not input_path.exists():
        sys.exit(f"Input not found: {input_path}")
    out = pathlib.Path(args.out).expanduser() if args.out else pathlib.Path.cwd() / "ocr_out"
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        items = gather(input_path, pathlib.Path(tmp), args.chrome)
        if not items:
            sys.exit(f"Nothing to OCR under {input_path} (no .pdf or .zip bundles found).")
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
