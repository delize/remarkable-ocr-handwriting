#!/usr/bin/env python3
"""
rm_ocr.py — transcribe handwriting with a local Qwen3-VL via Ollama.

Accepts ANY of:
  - a single .pdf, .zip, .rmdoc, .rm, .png, .jpg, .jpeg, or .webp file
  - a directory containing any mix of the above (searched recursively)

Bundles (.zip / .rmdoc) and loose pages (.rm) are rendered to PDF via the
shared rm_render module (which shells out to `rmc`). Images are wrapped into a
one-page PDF by the same module. PDFs are processed as-is.

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
import re
import sys
import tempfile
import urllib.request
from pdf2image import convert_from_path, pdfinfo_from_path

import rm_render
import rm_strokes

PROMPT = (
    "Transcribe all handwritten text on this page exactly as written. "
    "Preserve line breaks and rough layout. Output only the transcription as "
    "plain markdown, no commentary. If a word is genuinely illegible, write "
    "[illegible] rather than guessing at it."
)
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/api/generate"

# Vision models (esp. smaller local ones) tend to break the "no commentary"
# instruction on a genuinely blank page, answering with refusal-style prose
# ("I'm sorry, but there is no handwritten text visible...") instead of
# nothing — which then pollutes the transcript. Measured on real rendered
# reMarkable pages: a truly blank page comes back as flat mean=255/stddev=0;
# every page with actual content (even a single short line) measured
# stddev >= 18. These thresholds leave a wide margin on both sides.
BLANK_MEAN_THRESHOLD = 254.5
BLANK_STDDEV_THRESHOLD = 1.0
BLANK_PAGE_TEXT = "[blank page]"
# Written in place of a page the model answered with nothing. Distinct from
# BLANK_PAGE_TEXT on purpose: "the page was empty" and "the model failed on a
# page that had ink" must not look the same in a transcript.
NO_OUTPUT_TEXT = "[no transcription returned]"


def _is_blank_page(page):
    """Cheap pre-OCR check: is this PIL page image blank (or as good as)?

    Lazy import: pdf2image already pulls in Pillow for real use, but
    selftest.py stubs pdf2image out entirely to stay dependency-free, so this
    must not be a module-level import.
    """
    from PIL import ImageStat
    stat = ImageStat.Stat(page.convert("L"))
    return stat.mean[0] > BLANK_MEAN_THRESHOLD and stat.stddev[0] < BLANK_STDDEV_THRESHOLD


# A handwritten page wraps lines at the page edge, not at the end of a
# sentence or thought — asking the *model* to reflow that into paragraphs
# was tried and rejected: even a carefully-worded "don't paraphrase, just
# join wrapped lines" prompt measurably pushed a local vision model (minicpm-v)
# toward fabricating plausible-sounding prose instead of transcribing
# faithfully. Reflowing is instead a deterministic text transform applied
# *after* transcription, on text the model already produced — no re-reading
# of the image, so no new hallucination risk. It relies on the model's
# existing behavior of leaving a blank line between real paragraphs (already
# true of every model tried this session) while wrapping within one.
_STRUCTURAL_LINE = re.compile(r"^(#{1,6}\s|[-*+]\s|\d+\.\s|>\s)")


def reflow_paragraphs(text):
    """Join word-wrapped lines within a paragraph into flowing prose.

    A paragraph is a run of non-blank lines. Blank lines, headings, bullet /
    numbered list items, blockquotes, and fenced code blocks (``` ... ```,
    including any blank lines *inside* the fence) are left exactly as-is —
    only plain prose lines get joined with a space.
    """
    out_lines = []
    buffer = []
    in_fence = False

    def flush():
        if buffer:
            out_lines.append(" ".join(buffer))
            buffer.clear()

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            flush()
            out_lines.append(line)
        elif in_fence:
            out_lines.append(line)
        elif not stripped:
            flush()
            out_lines.append("")
        elif _STRUCTURAL_LINE.match(stripped):
            flush()
            out_lines.append(line)
        else:
            buffer.append(stripped)
    flush()
    return "\n".join(out_lines)


def ocr_pdf(pdf, model, dpi, max_px, cpu=False, timeout=1800, threads=None, no_think=False,
           skip_blank=True, page_regions=None, reflow=True, num_ctx=0):
    results = []
    empty_pages = 0
    opts = {"temperature": 0}
    if num_ctx:
        # Ollama defaults to a 4096 context. A full-page image already costs
        # ~1800 of those tokens, so a model that reasons before answering can
        # exhaust the window and be cut off with nothing in "response" (see the
        # empty-answer handling below). Measured on qwen3-vl:8b against a real
        # page: 4096 produced 0 chars, 16384 transcribed it correctly.
        opts["num_ctx"] = num_ctx
    if cpu:
        opts["num_gpu"] = 0   # 0 layers on GPU == CPU-only (num_gpu = #layers, not #GPUs)
    if threads:
        opts["num_thread"] = threads   # override Ollama's under-detected count (cgroup "max" bug)
    # Render one page at a time. convert_from_path over the whole document
    # rasterizes EVERY page into RAM at once (at `dpi`); on a long notebook at a
    # high DPI that alone can OOM-kill the container before a single page is even
    # sent to the model — a SIGKILL no try/except can catch, which then wedges
    # the daemon in a restart loop on that one file. pdfinfo gives the page count
    # cheaply, then each page is rendered, OCR'd, and freed in turn.
    num_pages = pdfinfo_from_path(str(pdf))["Pages"]
    for n in range(1, num_pages + 1):
        page = convert_from_path(str(pdf), dpi=dpi, first_page=n, last_page=n)[0]
        if skip_blank and _is_blank_page(page):
            print(f"    page {n}/{num_pages}: blank, OCR skipped", flush=True)
            results.append((n, BLANK_PAGE_TEXT))
            continue
        w, h = page.size
        s = min(1.0, max_px / max(w, h))
        if s < 1.0:
            page = page.resize((int(w * s), int(h * s)))
        buf = io.BytesIO()
        page.save(buf, format="PNG")
        prompt = PROMPT
        if page_regions and n - 1 < len(page_regions):
            hint = rm_strokes.prompt_hint(page_regions[n - 1])
            if hint:
                prompt = f"{PROMPT}\n\n{hint}"
        payload = {
            "model": model,
            "prompt": prompt,
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
        print(f"    page {n}/{num_pages} (prefill on CPU may take minutes)...", end="", flush=True)
        parts = []
        think_chars = 0
        done_reason = None
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
                # A reasoning model streams its trace here, NOT into "response".
                # Counted (not kept) purely so an empty answer can be explained.
                if obj.get("thinking"):
                    think_chars += len(obj["thinking"])
                if obj.get("done"):
                    done_reason = obj.get("done_reason")
                    break
        print(f" {len(''.join(parts))} chars", flush=True)
        text = "".join(parts).strip()
        if not text:
            # An empty answer used to be written out as an empty page under
            # status=ok, which is indistinguishable from a blank page and hides
            # a real failure. The common cause is a reasoning model that ignores
            # think=False, reasons past the context window, and is cut off before
            # emitting anything (done_reason="length", all output in "thinking").
            why = f"done_reason={done_reason!r}"
            if think_chars:
                why += f", {think_chars} chars of reasoning discarded"
            if done_reason == "length":
                why += (" — the model ran out of context before answering; raise"
                        " num_ctx (NUM_CTX) or use a non-reasoning model")
            print(f"    page {n}/{num_pages}: model returned NO TEXT ({why})", flush=True)
            text = f"{NO_OUTPUT_TEXT} ({why})"
            empty_pages += 1
        if reflow:
            text = reflow_paragraphs(text)
        results.append((n, text))
    # Every page failing is a broken run, not a transcript. Raising here puts it
    # through the daemon's normal error path (status=error, capped retries) so it
    # shows up in --status instead of landing as a plausible-looking empty file.
    # A partial failure still returns, with the bad pages marked in place.
    if empty_pages and empty_pages == len(results):
        raise RuntimeError(
            f"model returned no text for any of the {empty_pages} page(s) — "
            "see the per-page reasons above")
    return results


def transcribe_pdf(pdf, out_md, *, model, dpi=150, max_px=1568, threads=None,
                   no_think=False, timeout=1800, cpu=False, title=None,
                   page_regions=None, skip_blank=True, reflow=True, num_ctx=0):
    """Transcribe a single PDF to a plain ``# title`` / ``## Page N`` markdown file.

    Reusable core extracted from ``main()`` (Phase 0). The daemon does NOT call
    this — it writes its own frontmatter+backlink markdown — but it keeps the CLI
    path and any other caller on one code path, and returns the per-page metadata
    the manifest wants.

    ``page_regions`` (optional): rm_strokes per-page region hints from
    ``rm_render.RenderResult.page_regions`` — see ``ocr_pdf``.

    Returns a dict: ``{pages, chars_per_page, out_path, sketch_regions_total}``.
    """
    pdf = pathlib.Path(pdf)
    out_md = pathlib.Path(out_md)
    title = title or pdf.stem
    pages = ocr_pdf(pdf, model, dpi, max_px, cpu=cpu, timeout=timeout,
                    threads=threads, no_think=no_think, num_ctx=num_ctx,
                    skip_blank=skip_blank, page_regions=page_regions, reflow=reflow)
    lines = [f"# {title}\n"]
    for n, text in pages:
        lines.append(f"\n## Page {n}\n\n{text}\n")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines))
    sketch_regions_total = sum(
        rm_strokes.summarize(regions)["likely_drawing_regions"] for regions in page_regions
    ) if page_regions else 0
    return {
        "pages": len(pages),
        "chars_per_page": [len(text) for _, text in pages],
        "out_path": str(out_md),
        "sketch_regions_total": sketch_regions_total,
    }


def _safe(name):
    s = "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip()
    return s or "untitled"


def gather(input_path, work, cache_dir=None, extract_regions=False):
    """Return list of (title, pdf_path, page_regions) for everything to OCR under input_path.

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
                extract_regions=extract_regions,
            )
        except Exception as e:
            print(f"  [skip] {src.name}: {e}", file=sys.stderr)
            continue
        out.append((result.title, result.pdf, result.page_regions))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="A .pdf, .zip, .rmdoc, .rm, .png, .jpg, .jpeg or .webp file, "
                                  "or a folder containing any mix of those")
    ap.add_argument("--out", default=None, help="Output dir (default: ./ocr_out)")
    ap.add_argument("--model", default="qwen3-vl:8b")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--max-px", type=int, default=1568)
    ap.add_argument("--cpu", action="store_true", help="Force CPU-only (num_gpu=0) — simulates the GPU-less NAS")
    ap.add_argument("--timeout", type=int, default=1800, help="Per-page timeout in seconds (covers slow CPU prefill)")
    ap.add_argument("--threads", type=int, default=None, help="Force CPU thread count (e.g. 14 on a 13600K; works around Ollama's cgroup under-detection)")
    ap.add_argument("--no-think", action="store_true",
                    help="Ask the model to skip its reasoning trace. NOTE: some models "
                         "(qwen3-vl:8b measured) ignore this outright and reason anyway — "
                         "see --num-ctx if pages come back empty")
    ap.add_argument("--num-ctx", type=int, default=int(os.environ.get("NUM_CTX", "0")),
                    help="Model context window in tokens (0 = Ollama's default, 4096). A "
                         "page image costs ~1800, so a reasoning model can run out and "
                         "return nothing; 16384 fixed that on a real page.")
    ap.add_argument("--render-cache", default=os.environ.get("RM_OCR_RENDER_CACHE"),
                    help="Persistent render cache dir (default: ephemeral temp). Point at the daemon's STATE/rendered to share it.")
    ap.add_argument("--stroke-context", action="store_true",
                    help="Best-effort: parse .rm stroke geometry (rm_strokes) to hint the OCR "
                         "prompt about likely sketch/diagram regions. .rm-family inputs only "
                         "(no effect on plain .pdf); heuristic, not real handwriting recognition.")
    ap.add_argument("--no-skip-blank", action="store_false", dest="skip_blank",
                    help="Send genuinely blank pages to the model instead of skipping them "
                         "(default: skip — small models tend to answer blank pages with "
                         "refusal-style commentary instead of nothing)")
    ap.add_argument("--no-reflow", action="store_false", dest="reflow",
                    help="Keep the model's literal per-page-line breaks instead of joining "
                         "word-wrapped lines into flowing paragraphs (default: reflow). Pure "
                         "text post-processing on the model's own output, not a re-transcription.")
    args = ap.parse_args()

    input_path = pathlib.Path(args.input).expanduser()
    if not input_path.exists():
        sys.exit(f"Input not found: {input_path}")
    out = pathlib.Path(args.out).expanduser() if args.out else pathlib.Path.cwd() / "ocr_out"
    out.mkdir(parents=True, exist_ok=True)
    cache_dir = pathlib.Path(args.render_cache).expanduser() if args.render_cache else None

    with tempfile.TemporaryDirectory() as tmp:
        items = gather(input_path, pathlib.Path(tmp), cache_dir=cache_dir,
                       extract_regions=args.stroke_context)
        if not items:
            supported = " / ".join(sorted(rm_render.SUPPORTED_INPUT_SUFFIXES))
            sys.exit(f"Nothing to OCR under {input_path} (no {supported} found).")
        print(f"{len(items)} document(s). model={args.model} dpi={args.dpi}\nout: {out}\n")
        for title, pdf, page_regions in items:
            title = _safe(title)
            print(f"[{title}] OCR...", flush=True)
            try:
                transcribe_pdf(
                    pdf, out / f"{title}.md",
                    model=args.model, dpi=args.dpi, max_px=args.max_px,
                    threads=args.threads, no_think=args.no_think,
                    timeout=args.timeout, cpu=args.cpu, title=title,
                    page_regions=page_regions,
                    skip_blank=args.skip_blank,
                    reflow=args.reflow,
                    num_ctx=args.num_ctx,
                )
                print(f"        -> {title}.md\n", flush=True)
            except Exception as e:
                print(f"        FAILED: {e}\n", flush=True)

    print(f"Done. Transcripts in {out}")


if __name__ == "__main__":
    main()
