#!/usr/bin/env python3
"""
ocr_daemon.py — watch the Scrybble-synced reMarkable PDFs in an Obsidian vault and
transcribe new/changed ones into a separate, searchable transcripts tree.

Build the automation around the *proven* OCR core in rm_ocr.py — never rewrite it.
See the build brief for the settled hardware/model/run-setting decisions.

Modes:
  python3 ocr_daemon.py            # daemon: scan -> process serially -> sleep INTERVAL, forever
  python3 ocr_daemon.py --scan     # one-shot: a single incremental pass, then exit (cron/systemd)
  python3 ocr_daemon.py --status   # print the manifest summary and exit

All configuration is via environment variables (see the table in the brief / README).
"""
import argparse
import base64
import datetime
import hashlib
import json
import logging
import os
import pathlib
import re
import sys
import time

from collections import Counter

import rm_ocr
import rm_render
import rm_strokes
import rm_verify
from rm_ocr import BLANK_PAGE_TEXT, NO_OUTPUT_TEXT, _safe, ocr_pdf  # reuse the proven core
from rm_split import SplitConfig, split_in_place


# ---------------------------------------------------------------------------
# Config (env / .env)
# ---------------------------------------------------------------------------
def _env_bool(name, default):
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


VAULT = pathlib.Path(os.environ.get("VAULT_DIR", "/vault"))
SRC = VAULT / os.environ.get("SOURCE_SUBDIR", "remarkable")
# Output base. Prefer an independent volume-mount base (OUT_DIR) so transcripts
# live OUTSIDE the read-only vault (no rw sub-mount, no scan feedback loop).
# Fall back to a subdir inside the vault (OUT_SUBDIR) for backward compatibility.
# Either way, transcripts mirror the source subpath under the base.
if os.environ.get("OUT_DIR"):
    OUT = pathlib.Path(os.environ["OUT_DIR"])
else:
    OUT = VAULT / os.environ.get("OUT_SUBDIR", "remarkable/_transcripts")
# Filename = <source stem><OUT_SUFFIX>.md, e.g. "Sample-handwriting_converted.md".
OUT_SUFFIX = os.environ.get("OUT_SUFFIX", "-handwriting_converted")
# Alongside mode: write each transcript into the SAME folder as its source PDF,
# instead of mirroring under OUT_DIR. Needs a writable vault and a non-empty
# OUT_SUFFIX (so we never collide with a source PDF or a Scrybble .md stub).
OUT_ALONGSIDE = _env_bool("OUT_ALONGSIDE", False)
STATE = pathlib.Path(os.environ.get("STATE_DIR", "/state"))
MANIFEST = STATE / "manifest.json"
LOGFILE = STATE / "ocr.log"

MODEL = os.environ.get("MODEL", "gemma4:26b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
# Block at startup until the model is loadable on OLLAMA_HOST. 0 disables the gate
# (useful for tests / non-ollama setups). The default headroom accommodates a
# cold pull on a slow link plus the first CPU model-load.
MODEL_WAIT_TIMEOUT = int(os.environ.get("MODEL_WAIT_TIMEOUT", "1800"))
# Handwriting never leaves the owner's machines: at startup the model host must
# resolve only to loopback, private LAN, link-local or 100.64/10 (Tailscale)
# addresses. ALLOW_REMOTE_MODEL_HOST=1 downgrades the refusal to a warning.
ALLOW_REMOTE_MODEL_HOST = _env_bool("ALLOW_REMOTE_MODEL_HOST", False)
THREADS = int(os.environ.get("THREADS", "14"))
NO_THINK = _env_bool("NO_THINK", True)
# Skip the OCR call entirely for a page that's genuinely blank (rm_ocr's cheap
# pixel-stat pre-check). Small local vision models tend to answer a blank page
# with refusal-style prose instead of nothing, which pollutes the transcript —
# on by default since there's nothing useful to lose by skipping.
SKIP_BLANK_PAGES = _env_bool("SKIP_BLANK_PAGES", True)
# Join word-wrapped lines within a paragraph into flowing prose (rm_ocr's
# reflow_paragraphs) — pure text post-processing on the model's own output,
# not a re-transcription, so it can't introduce new hallucination. On by
# default; the model's literal per-page-line output is one env flag away.
REFLOW_PARAGRAPHS = _env_bool("REFLOW_PARAGRAPHS", True)
DPI = int(os.environ.get("DPI", "150"))
MAX_PX = int(os.environ.get("MAX_PX", "1568"))
TIMEOUT = int(os.environ.get("TIMEOUT", "1800"))
# Context window for the model, in tokens (0 = leave it to Ollama, which
# defaults to 4096). A full-page image alone costs roughly 1800 of those, so a
# model that reasons before answering can burn the rest of the window and get
# cut off mid-thought, returning NOTHING. Measured on qwen3-vl:8b against a real
# faint reMarkable page: 4096 -> 0 chars on every page, 16384 -> a correct
# transcript. Costs VRAM, so it is opt-in rather than defaulted.
NUM_CTX = int(os.environ.get("NUM_CTX", "0"))
# Startup gate: prove the model actually RECEIVES the images we send. A runner
# that drops them silently (Ollama 0.32.0's MLX runner does) makes the model
# answer from the prompt alone and invent a fluent transcript that looks
# perfectly successful. Compares prompt token counts across two image SIZES, so
# it tests delivery rather than OCR skill. Costs three 1-token generates.
VISION_CHECK = _env_bool("VISION_CHECK", True)
# Minimum extra prompt tokens a 1024x1024 image must cost over a 64x64 one.
# Measured growth: qwen3.5:9b 1015, gemma4:26b 207; a runner that drops images
# shows ~0. 64 sits an order of magnitude clear of the failure case.
VISION_CHECK_MIN_TOKENS = int(os.environ.get("VISION_CHECK_MIN_TOKENS", "64"))
INTERVAL = int(os.environ.get("INTERVAL", "600"))
# Inotify wake-up signal layered on top of the poll. The poll stays as a
# correctness floor (so a missed event never strands a file forever), but a
# CLOSE_WRITE / MOVED_TO on a *.pdf under SRC short-circuits the next pass —
# typical latency drops from <=INTERVAL to <1s. Linux-only (needs inotify_simple
# + a backing filesystem that supports inotify; ext4/btrfs/zfs do, SMB/NFS often
# don't). Falls back to pure poll if the import fails or watches can't be added.
INOTIFY = _env_bool("INOTIFY", True)
HASH_CHECK = _env_bool("HASH_CHECK", True)
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
# Only consider PDFs modified within this many hours (0 = no age limit). Bounds
# the working set to recent edits so old, already-handled notes are never even
# re-statted, and a first run on a full vault doesn't transcribe the entire
# backlog. Run once with MAX_AGE_HOURS=0 to deliberately backfill everything.
MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", "24"))
# Refuse to reprocess the same path more often than this many seconds, even when
# its bytes changed (0 = off). Safety net against a source that re-renders
# non-deterministically (new sha256 every sync) and would otherwise loop forever.
MIN_REPROCESS_INTERVAL = float(os.environ.get("MIN_REPROCESS_INTERVAL", "0"))
# Skip documents with more pages than this (0 = no limit). A synced store
# template or imported book can run to hundreds of pages — days of CPU OCR for
# content that is rarely handwriting. Counted on the rendered, post-AUTO_SPLIT
# PDF (what OCR would actually see). The manifest records the count under
# status=skipped_pages, and the gate re-checks the recorded count against the
# CURRENT cap, so raising or disabling MAX_PDF_PAGES re-queues those files on
# the next pass with no touch needed.
MAX_PDF_PAGES = int(os.environ.get("MAX_PDF_PAGES", "0"))
# Optional politeness window, e.g. "01:00-07:00". Empty = always run.
RUN_WINDOW = os.environ.get("RUN_WINDOW", "").strip()

# Split-readiness gate (opt-in). reMarkable exports can be a single very tall page
# that the vision model can't read; the companion remarkable-pdf-splitter
# (github.com/delize/remarkable-pdf-splitter) breaks them into readable pages and
# stamps a /RemarkableSplitter Info-dict marker. With REQUIRE_SPLIT=1 we only OCR a
# PDF once it is "ready" = it carries that marker OR no page exceeds the aspect
# ratio (i.e. it never needed splitting). Off by default so the tool works without
# the splitter.
REQUIRE_SPLIT = _env_bool("REQUIRE_SPLIT", False)
SPLIT_MARKER_KEY = os.environ.get("SPLIT_MARKER_KEY", "/RemarkableSplitter")
SPLIT_MARKER_VALUE = os.environ.get("SPLIT_MARKER_VALUE", "processed")
# Must match the splitter's MIN_ASPECT_RATIO (height/width). A taller page with no
# marker is treated as not-yet-split.
SPLIT_MAX_ASPECT = float(os.environ.get("SPLIT_MAX_ASPECT", "2.0"))

# AUTO_SPLIT: do the splitting ourselves (one tool, split -> OCR in one pass)
# instead of waiting on the standalone splitter. Splits the source PDF IN PLACE
# (so the readable split PDF also persists), then OCRs it. Requires the source
# dir to be WRITABLE (not the usual :ro vault mount) and pulls in PyMuPDF +
# numpy. Implies split-readiness, so REQUIRE_SPLIT's gate is moot when this is on.
AUTO_SPLIT = _env_bool("AUTO_SPLIT", False)
SPLIT_TARGET_PAGE_HEIGHT = int(os.environ.get("SPLIT_TARGET_PAGE_HEIGHT", "700"))
SPLIT_MIN_GAP_HEIGHT = int(os.environ.get("SPLIT_MIN_GAP_HEIGHT", "25"))
SPLIT_WHITESPACE_THRESHOLD = int(os.environ.get("SPLIT_WHITESPACE_THRESHOLD", "248"))
SPLIT_MAX_SEGMENT_FACTOR = float(os.environ.get("SPLIT_MAX_SEGMENT_FACTOR", "2.0"))

# STROKE_CONTEXT (opt-in): parse each source .rm page's stroke geometry
# (rm_strokes) into a rough "probably a sketch, not text" hint per page, added
# to the OCR prompt and summarized in the transcript frontmatter. Heuristic
# (size/shape of stroke clusters), not real handwriting recognition — no open,
# offline ink-to-text engine exists to pair with the vision model. Only
# possible for .rm/.rmdoc/.zip sources (a plain .pdf never carries stroke
# data). Needs rmscene, which is normally already present transitively via rmc.
STROKE_CONTEXT = _env_bool("STROKE_CONTEXT", False)

# DAILY_NOTE_EMBED (opt-in): after successfully transcribing a source whose
# title is a plain date (YYYY-MM-DD — the shape a daily-journal tool exports),
# ensure the matching Obsidian daily note `<VAULT>/<DAILY_NOTE_DIR>/<date>.md`
# contains a section embedding the transcript. Transclusion, not copying: the
# note gets one `![[<vault-relative transcript path>]]` line, so when a later
# re-OCR overwrites the transcript the note's rendered content updates with no
# further write here. Append-only and idempotent — existing prose is never
# rewritten, and a note that already references the transcript path (our
# section or a hand-written link) is left alone. Needs the vault mounted
# writable, and only works when transcripts land INSIDE the vault
# (OUT_ALONGSIDE, or OUT under VAULT); an outside-the-vault OUT_DIR can't be
# transcluded by Obsidian, so the embed is skipped with a warning.
DAILY_NOTE_EMBED = _env_bool("DAILY_NOTE_EMBED", False)
DAILY_NOTE_DIR = os.environ.get("DAILY_NOTE_DIR", "Daily Journal")
DAILY_NOTE_HEADING = os.environ.get("DAILY_NOTE_HEADING", "## reMarkable journal")

# --- Self-checking transcripts (rm_verify, rm_eval) ---
# VOCAB_FILE: the writer's own terms, comma or newline separated. When present
# the OCR prompt asks the model to prefer these spellings for ambiguous words.
# Measured on 16 jargon-heavy pages: WER 9.6% -> 8.6% at no speed cost.
VOCAB_FILE = pathlib.Path(os.environ.get("VOCAB_FILE", str(STATE / "vocab.txt")))
# VERIFY_MODEL (opt-in): transcribe each page a second time with this model and
# flag words the two readings disagree on as ==A|B== highlights. Measured on 26
# pages, flags covered ~6% of words and fixing just those took WER from ~6.2%
# to ~2.2%. Doubles OCR time per page, so VERIFY_PATHS can limit it to some
# folders (comma-separated, relative to SOURCE_SUBDIR, e.g. "Work,Meeting Notes").
VERIFY_MODEL = os.environ.get("VERIFY_MODEL", "").strip()
VERIFY_PATHS = tuple(p.strip().strip("/") for p in os.environ.get("VERIFY_PATHS", "").split(",")
                     if p.strip())
# VERIFY_RESOLVE (opt-in): put each disagreement back to a model with the page
# image as a constrained choice (A, B, the exact text, or unsure) instead of
# flagging it. Measured on 26 pages it answered 88 of 90 questions and was
# barely better than chance: WER 5.4% -> 5.1% unattended, at ~90 s extra per
# page, and it removed the flags that let review reach 1.9%. Flags stay the
# default. RESOLVE_MODEL defaults to VERIFY_MODEL, already loaded at that point.
VERIFY_RESOLVE = _env_bool("VERIFY_RESOLVE", False)
RESOLVE_MODEL = os.environ.get("RESOLVE_MODEL", "").strip() or VERIFY_MODEL
VERIFY_MAX_SPAN_WORDS = int(os.environ.get("VERIFY_MAX_SPAN_WORDS", "6"))
# VERIFY_UNLOAD: drop one model before loading the other, for a CPU host that
# cannot hold both (two ~20 GB models on a box with ~35 GB free).
VERIFY_UNLOAD = _env_bool("VERIFY_UNLOAD", True)
# LEARN_CORRECTIONS: remember what was written to each transcript (a sidecar
# under STATE/transcripts), notice when it is edited in Obsidian, log the
# corrections, store edited pages as ground truth under GOLDSET_DIR, and keep
# those edits when a re-OCR produces the same text for the page. Only reads
# transcripts and writes under STATE, so it is on by default.
LEARN_CORRECTIONS = _env_bool("LEARN_CORRECTIONS", True)
GOLDSET_DIR = pathlib.Path(os.environ.get("GOLDSET_DIR", str(STATE / "goldset")))
# USE_LEARNED_VOCAB (opt-in): add terms learned from corrections to the prompt
# hint. A term qualifies after LEARN_MIN_COUNT separate corrections, and with
# LEARN_GATE it is only activated when an evaluation on up to LEARN_GATE_PAGES
# ground-truth pages shows the primary model is no worse with it.
USE_LEARNED_VOCAB = _env_bool("USE_LEARNED_VOCAB", False)
LEARN_MIN_COUNT = int(os.environ.get("LEARN_MIN_COUNT", "2"))
LEARN_GATE = _env_bool("LEARN_GATE", True)
LEARN_GATE_PAGES = int(os.environ.get("LEARN_GATE_PAGES", "10"))
LEARNED_VOCAB = STATE / "learned_vocab.json"
CORRECTIONS_LOG = STATE / "corrections.jsonl"
EVAL_HISTORY = STATE / "eval" / "history.jsonl"

# Absolute paths the daemon must NEVER read or write under, no matter what.
# Comma-separated override via FORBIDDEN_PATHS; default protects the standalone
# Scrybble container's auth-credential storage in case both tools run on the
# same host. Set to empty to disable the guard entirely (not recommended).
FORBIDDEN_PREFIXES = tuple(
    p.strip() for p in os.environ.get(
        "FORBIDDEN_PATHS",
        "/mnt/docker/scrybble/storage",
    ).split(",") if p.strip()
)

log = logging.getLogger("rm-ocr")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging():
    STATE.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    # LOG_LEVEL=DEBUG surfaces the per-file gate decisions (prefilter-skip /
    # hash-unchanged / retry-capped / queued) so you can watch what runs OCR.
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    log.setLevel(level)
    log.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    try:
        fh = logging.FileHandler(LOGFILE)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as e:
        log.warning("could not open log file %s: %s", LOGFILE, e)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def load_manifest():
    try:
        return json.loads(MANIFEST.read_text())
    except Exception:
        return {}


def save_manifest(man):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(man, indent=2, sort_keys=True))
    tmp.replace(MANIFEST)  # atomic: never leave a half-written manifest


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Safety guards (§1, §2 of the brief)
# ---------------------------------------------------------------------------
def assert_safe_paths():
    """Fail fast on a misconfiguration that would point us at protected data."""
    if OUT_ALONGSIDE and not OUT_SUFFIX:
        raise SystemExit("OUT_ALONGSIDE requires a non-empty OUT_SUFFIX "
                         "(otherwise a transcript could overwrite a source PDF or Scrybble .md stub)")
    paths = [VAULT.resolve(), SRC.resolve(), STATE.resolve()]
    if not OUT_ALONGSIDE:
        paths.append(OUT.resolve())
    for path in paths:
        for forbidden in FORBIDDEN_PREFIXES:
            if str(path) == forbidden or str(path).startswith(forbidden.rstrip("/") + "/"):
                raise SystemExit(f"refusing to operate under forbidden path: {path}")
    # Writing into the source tree is allowed ONLY when a suffix guarantees the
    # transcript name can't equal a source/stub name.
    if not OUT_ALONGSIDE and OUT.resolve() == SRC.resolve() and not OUT_SUFFIX:
        raise SystemExit("output dir equals source dir with empty OUT_SUFFIX — would overwrite sources")
    # Daily notes must live OUTSIDE the synced source tree: `<date>.md` files
    # inside it belong to the sync tool (e.g. Scrybble stubs) and would be
    # clobbered on the next sync — and our append would fight that writer.
    if DAILY_NOTE_EMBED:
        daily_dir = (VAULT / DAILY_NOTE_DIR).resolve()
        if daily_dir == SRC.resolve() or SRC.resolve() in daily_dir.parents:
            raise SystemExit("DAILY_NOTE_DIR must be outside SOURCE_SUBDIR "
                             "(date-named .md files in the source tree belong to the sync tool)")


def safe_output_path(src, title=None, *, source_sha256=None):
    """Map a source file to its transcript path and prove the result is safe to write.

    Filename is ``<safe(title)><OUT_SUFFIX>.md``. ``title`` defaults to ``src.stem``
    (the historical PDF behavior); bundles pass the visibleName-derived title from
    rm_render so the transcript isn't named after a uuid. In alongside mode the
    transcript sits in the source file's own folder; otherwise it mirrors the
    source subpath under OUT. Guarantees the target is a .md, never equals the
    source, never lands under a forbidden prefix, and (mirror mode) stays strictly
    under OUT.

    Collision handling: if a transcript with this exact path already exists but
    its frontmatter ``source:`` line points at a different rel, append
    ``-<source_sha256[:8]>`` to the stem. Only kicks in when ``source_sha256`` is
    supplied (production callers) — keeps the test fixtures simple.
    """
    if title is None:
        title = src.stem
    safe_title = _safe(title)
    name = safe_title + OUT_SUFFIX + ".md"
    if OUT_ALONGSIDE:
        out_md = src.with_name(name)
    else:
        rel = src.relative_to(SRC)
        out_md = OUT / rel.parent / name

    if source_sha256 and out_md.exists():
        try:
            head = out_md.read_text()[:512]
            rel_str = str(src.relative_to(VAULT))
            if f"source: {rel_str}" not in head:
                # Different bundle, same visibleName — disambiguate by content hash.
                name = f"{safe_title}-{source_sha256[:8]}{OUT_SUFFIX}.md"
                out_md = src.with_name(name) if OUT_ALONGSIDE else OUT / rel.parent / name
        except OSError:
            pass

    out_res = out_md.resolve()
    if out_res.suffix.lower() != ".md":
        raise ValueError(f"refusing non-.md output: {out_md}")
    if out_res == src.resolve():
        raise ValueError(f"output path would overwrite the source: {out_md}")
    for forbidden in FORBIDDEN_PREFIXES:
        if str(out_res) == forbidden or str(out_res).startswith(forbidden.rstrip("/") + "/"):
            raise ValueError(f"output path under forbidden prefix: {out_md}")
    if not OUT_ALONGSIDE and OUT.resolve() not in out_res.parents:
        raise ValueError(f"output path escapes OUT_DIR: {out_md}")
    return out_md


def is_under_out(pdf):
    """True if this PDF lives inside the transcripts tree (don't transcribe our own tree)."""
    try:
        pdf.resolve().relative_to(OUT.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Split-readiness gate
# ---------------------------------------------------------------------------
def _pdf_split_info(pdf):
    """Return (marker_value, max_aspect_ratio) for a PDF, read with pypdf.

    Isolated so the offline self-test can monkeypatch it without pypdf or a real
    PDF. ``marker_value`` is the /RemarkableSplitter Info-dict value (or None);
    ``max_aspect_ratio`` is the tallest page's height/width.
    """
    from pypdf import PdfReader  # lazy: only needed when the gate is on
    reader = PdfReader(str(pdf))
    md = reader.metadata or {}
    marker = md.get(SPLIT_MARKER_KEY)
    max_ar = 0.0
    for page in reader.pages:
        box = page.mediabox
        w, h = float(box.width), float(box.height)
        if w:
            max_ar = max(max_ar, h / w)
    return marker, max_ar


def split_ready(pdf, rel):
    """True if this PDF is safe to OCR w.r.t. the split gate.

    Ready = gate off, OR it carries the splitter's marker, OR no page is tall
    enough to have needed splitting. A read failure is treated as not-ready (we'd
    rather wait than feed the model an unreadable page).
    """
    if not REQUIRE_SPLIT:
        return True
    try:
        marker, max_ar = _pdf_split_info(pdf)
    except Exception as e:
        log.warning("split-check failed for %s: %s (treating as not ready)", rel, e)
        return False
    if marker == SPLIT_MARKER_VALUE:
        return True
    if max_ar <= SPLIT_MAX_ASPECT:
        return True  # never needed splitting
    log.debug("gate=pending-split %s (aspect %.2f > %.2f, no %s marker)",
              rel, max_ar, SPLIT_MAX_ASPECT, SPLIT_MARKER_KEY)
    return False


# ---------------------------------------------------------------------------
# Change detection (§3)
# ---------------------------------------------------------------------------
def needs_work(pdf, rel, man):
    """Return the change-token if the file needs (re)processing, else False.

    Two detection modes, both keyed on the LOCAL rendered PDF (never the cloud):
      * HASH_CHECK=1 (default): mtime+size is a cheap pre-filter, sha256 is the
        authoritative signal. A byte-identical re-sync (new mtime, same bytes) is
        skipped.
      * HASH_CHECK=0: last-modified mode — the change token is the PDF's mtime
        (paired with size for robustness), so any last-modified bump reprocesses.
        Cheaper (no full hash) but re-OCRs on touch-only changes.
    """
    st = pdf.stat()
    rec = man.get(rel)
    if rec and rec.get("status") == "ok" \
       and rec.get("mtime") == st.st_mtime and rec.get("size") == st.st_size:
        log.debug("gate=prefilter-skip %s (mtime+size unchanged, no hash, no OCR)", rel)
        return False  # cheap pre-filter passed, nothing changed

    # HASH_CHECK=1 -> content hash; HASH_CHECK=0 -> last-modified (mtime) token.
    # Only reached when mtime or size moved, so the hash read is the exception.
    digest = sha256(pdf) if HASH_CHECK else f"mtime:{st.st_mtime}:{st.st_size}"

    if rec and rec.get("sha256") == digest:
        if rec.get("status") == "ok":
            rec["mtime"], rec["size"] = st.st_mtime, st.st_size  # touch-only change
            log.debug("gate=hash-unchanged %s (touched but bytes identical, no OCR)", rel)
            return False
        # Same bytes, still waiting on the splitter: don't re-check or re-log every
        # pass. A real change (splitter ran) bumps the hash and falls through.
        if rec.get("status") == "pending_split":
            log.debug("gate=still-pending-split %s (unchanged bytes, awaiting split)", rel)
            return False
        # Same bytes, previously over the page cap: stay skipped while the
        # RECORDED count still exceeds the CURRENT cap — raising or disabling
        # MAX_PDF_PAGES makes this fall through and re-queue automatically.
        if rec.get("status") == "skipped_pages":
            if MAX_PDF_PAGES > 0 and rec.get("page_count", 0) > MAX_PDF_PAGES:
                log.debug("gate=skipped-pages %s (%s pages > cap %d, no OCR)",
                          rel, rec.get("page_count"), MAX_PDF_PAGES)
                return False
        # Same bytes, but last attempt errored: respect the retry cap.
        if rec.get("retries", 0) >= MAX_RETRIES:
            log.debug("gate=retry-capped %s (errored %d times, no OCR)", rel, rec.get("retries", 0))
            return False

    # Bytes changed (or first sight). Optional cooldown: if we processed this same
    # path very recently, don't churn on it again — protects against a source that
    # keeps emitting byte-different renders of an unchanged note.
    if rec and MIN_REPROCESS_INTERVAL > 0 and rec.get("processed_at"):
        try:
            last = datetime.datetime.fromisoformat(rec["processed_at"]).timestamp()
        except ValueError:
            last = 0.0
        if time.time() - last < MIN_REPROCESS_INTERVAL:
            log.info("cooldown: %s changed but reprocessed %.0fs ago, skipping",
                     rel, time.time() - last)
            return False
    log.debug("gate=queued %s (changed -> will OCR)", rel)
    return digest


# ---------------------------------------------------------------------------
# Output writer (§2)
# ---------------------------------------------------------------------------
def _iso_mtime(st):
    """Source PDF's last-modified time as a local ISO-8601 timestamp."""
    return datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")


def write_md(out_md, title, rel, pages, source_modified=None, stroke_regions_flagged=None,
             verify=None, kept_edits=0):
    out_md.parent.mkdir(parents=True, exist_ok=True)
    chars = [len(text) for _, text in pages]
    verify = verify or {}
    resolved = sum(verify.get(k, 0) for k in ("resolved_a", "resolved_b", "resolved_other"))
    fm = [
        "---",
        f"source: {rel}",
        f"model: {MODEL}",
        f"source_modified: {source_modified}" if source_modified else None,
        f"processed_at: {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"pages: {len(pages)}",
        f"chars_per_page: {json.dumps(chars)}",
        f"stroke_regions_flagged: {stroke_regions_flagged}" if stroke_regions_flagged is not None else None,
        f"verify_model: {VERIFY_MODEL}" if verify else None,
        f"verify_resolved: {resolved}" if verify else None,
        # Words the two readings still disagree on, highlighted as ==A|B== in the text.
        f"verify_flagged: {verify.get('flagged', 0)}" if verify else None,
        f"verify_error: {json.dumps(verify['error'])}" if verify.get("error") else None,
        f"kept_edits: {kept_edits}" if kept_edits else None,
        "status: ok",
        "---",
        "",
    ]
    fm = [line for line in fm if line is not None]
    body = [f"# {title}", "", f"Source: [[{rel}]]", ""]
    for n, text in pages:
        body += [f"## Page {n}", "", text, ""]
    out_md.write_text("\n".join(fm + body))
    return chars


def _pdf_page_count(pdf):
    """Page count of a PDF via pdfinfo (cheap — no rasterization)."""
    from pdf2image import pdfinfo_from_path  # lazy: stubbed out in the offline self-test
    return int(pdfinfo_from_path(str(pdf))["Pages"])


# ---------------------------------------------------------------------------
# Daily-note embed (opt-in, DAILY_NOTE_EMBED)
# ---------------------------------------------------------------------------
# A daily-journal source is titled either `YYYY-MM-DD` (one file per day) or
# `YYYY-MM-DD-P<n>` (one file per PAGE of that day, the shape a multi-page photo
# or per-page export produces). Both route to the same `<date>.md` daily note;
# the page suffix only distinguishes the transcripts from each other.
_DAILY_NOTE_TITLE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-P\d+)?$", re.IGNORECASE)


def _insert_into_section(text, heading, link_line):
    """Append `link_line` inside `heading`'s existing section, or add the section.

    Multi-page days (`<date>-P001`, `-P002`, ...) embed one link per page into
    the SAME note, so a plain append would stack a duplicate heading per page.
    When the heading is already there the link joins that section instead, which
    also keeps the pages contiguous and in processing order (P001 before P002).
    Anything the human wrote after the section is left where it is.
    """
    if not text.strip() or heading not in text:
        section = f"{heading}\n\n{link_line}\n"
        return (text.rstrip("\n") + "\n\n" if text.strip() else "") + section

    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.strip() == heading.strip())
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    if level:  # a real markdown heading — its section ends at the next same-or-higher one
        for i in range(start + 1, len(lines)):
            stripped = lines[i].lstrip()
            if stripped.startswith("#"):
                if len(stripped) - len(stripped.lstrip("#")) <= level:
                    end = i
                    break
    tail = end
    while tail > start + 1 and not lines[tail - 1].strip():
        tail -= 1  # step back over blank lines so the link lands with its siblings
    lines[tail:tail] = [link_line]
    return "\n".join(lines)


def embed_in_daily_note(out_md, title):
    """Ensure the Obsidian daily note for a date-named source embeds its transcript.

    Accepts `YYYY-MM-DD` and `YYYY-MM-DD-P<n>` titles; both target the same
    `<date>.md` note, so every page of a multi-page day lands in one place.

    Returns True if the note was written (created or appended to), False for
    every no-op or skip. The embed uses the transcript's FULL vault-relative
    path — a bare basename would be ambiguous in vaults where `<date>.md`
    exists both as a daily note and as a sync-tool stub. The idempotency check
    is that same path string, so a hand-written link to the transcript also
    counts as "already embedded". Writes go through a temp file + os.replace,
    so a crash mid-write can never truncate a human's daily note.
    """
    matched = _DAILY_NOTE_TITLE.match(title)
    if not matched:
        return False
    note_date = matched.group(1)
    try:
        target = str(out_md.resolve().relative_to(VAULT.resolve()))
    except ValueError:
        log.warning("daily-note embed: transcript %s is outside the vault — "
                    "Obsidian can't transclude it, skipping", out_md)
        return False
    if target.endswith(".md"):
        target = target[: -len(".md")]
    note = (VAULT / DAILY_NOTE_DIR / f"{note_date}.md").resolve()
    for forbidden in FORBIDDEN_PREFIXES:
        if str(note) == forbidden or str(note).startswith(forbidden.rstrip("/") + "/"):
            log.warning("daily-note embed: %s under forbidden prefix, skipping", note)
            return False
    if VAULT.resolve() not in note.parents:
        log.warning("daily-note embed: %s escapes the vault, skipping", note)
        return False
    text = note.read_text() if note.exists() else ""
    if target in text:
        return False  # already embedded (or hand-linked) — never duplicate
    new_text = _insert_into_section(text, DAILY_NOTE_HEADING, f"![[{target}]]")
    note.parent.mkdir(parents=True, exist_ok=True)
    tmp = note.with_name(note.name + ".rm-embed.tmp")
    tmp.write_text(new_text)
    tmp.replace(note)
    # note is resolved; compare against the resolved vault too (macOS tmp dirs
    # are symlinks, /var -> /private/var, and the unresolved form would throw).
    log.info("daily-note embed: ![[%s]] -> %s", target, note.relative_to(VAULT.resolve()))
    return True


# ---------------------------------------------------------------------------
# Scan / process
# ---------------------------------------------------------------------------
def in_run_window():
    if not RUN_WINDOW:
        return True
    try:
        start_s, end_s = RUN_WINDOW.split("-")
        now = datetime.datetime.now().time()
        start = datetime.time.fromisoformat(start_s.strip())
        end = datetime.time.fromisoformat(end_s.strip())
    except Exception:
        log.warning("ignoring malformed RUN_WINDOW=%r", RUN_WINDOW)
        return True
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end  # window wraps midnight


# ---------------------------------------------------------------------------
# Self-checking transcripts (VOCAB_FILE, VERIFY_MODEL, LEARN_CORRECTIONS)
# ---------------------------------------------------------------------------
def current_terms():
    """The vocabulary hint's terms: VOCAB_FILE, plus active learned terms when enabled."""
    terms = []
    try:
        if VOCAB_FILE.is_file():
            terms = rm_verify.parse_terms(VOCAB_FILE.read_text())
    except OSError as e:
        log.warning("vocab file %s unreadable: %s", VOCAB_FILE, e)
    if USE_LEARNED_VOCAB:
        seen = {t.lower() for t in terms}
        terms += [t for t in rm_verify.LearnedVocab(LEARNED_VOCAB).active() if t.lower() not in seen]
    return terms


def verify_applies(src):
    """Dual read this source? Needs VERIFY_MODEL, and a matching VERIFY_PATHS prefix when set."""
    if not VERIFY_MODEL:
        return False
    if not VERIFY_PATHS:
        return True
    try:
        rel = src.relative_to(SRC).as_posix()
    except ValueError:
        return False
    return any(rel == p or rel.startswith(p + "/") for p in VERIFY_PATHS)


def _unload(model):
    try:
        rm_ocr.unload_model(model)
    except Exception as e:  # costs memory at worst, never the transcript
        log.warning("could not unload %s: %s", model, e)


def dual_read(pdf, pages, hint):
    """Second reading with VERIFY_MODEL, then resolve or flag each disagreement.

    Returns ``(pages, stats)``. A failure of the second reading keeps the
    primary text and records the error in the stats instead of failing the
    document: an unverified transcript is still better than none.
    """
    if VERIFY_UNLOAD and VERIFY_MODEL != MODEL:
        _unload(MODEL)
    try:
        second = dict(ocr_pdf(pdf, VERIFY_MODEL, DPI, MAX_PX, timeout=TIMEOUT, threads=THREADS,
                              no_think=NO_THINK, skip_blank=SKIP_BLANK_PAGES,
                              reflow=REFLOW_PARAGRAPHS, num_ctx=NUM_CTX, prompt_extra=hint))
    except Exception as e:
        log.warning("verify: second reading with %s failed: %s (keeping the primary text)",
                    VERIFY_MODEL, e)
        return pages, {"error": f"second reading: {e}"}
    totals = Counter()
    out = []
    for n, text in pages:
        other = second.get(n)
        if (other is None or text == BLANK_PAGE_TEXT or text.startswith(NO_OUTPUT_TEXT)
                or other.startswith(NO_OUTPUT_TEXT)):
            out.append((n, text))
            continue
        resolver = None
        if VERIFY_RESOLVE:
            def resolver(spans, n=n):
                try:
                    img = rm_ocr.render_page_b64(pdf, n, DPI, MAX_PX)
                    return rm_ocr.generate_json(
                        RESOLVE_MODEL, rm_verify.resolve_prompt(spans, hint), img,
                        rm_verify.RESOLVE_SCHEMA, timeout=TIMEOUT, threads=THREADS,
                        num_ctx=NUM_CTX or 16384)
                except Exception as e:  # unresolved spans are simply flagged
                    log.warning("verify: resolving page %d failed: %s", n, e)
                    return None
        text, stats = rm_verify.verify_page(text, other, resolver=resolver,
                                            max_span_words=VERIFY_MAX_SPAN_WORDS)
        totals.update(stats)
        out.append((n, text))
    if VERIFY_UNLOAD:
        for m in {VERIFY_MODEL, RESOLVE_MODEL} - {MODEL}:
            _unload(m)
    return out, dict(totals)


def _out_md_path(out_rel):
    p = pathlib.Path(out_rel)
    if p.is_absolute():
        return p
    for base in (OUT, VAULT):
        if (base / p).exists():
            return base / p
    return OUT / p


def _page_png(rel, rec, page):
    """The page image OCR saw, re-rendered from the source, or None if it has changed since."""
    try:
        if rec.get("render_sha256"):
            pdf = rm_render._cache_path(STATE / "rendered", rec.get("sha256", ""))
            if not pdf.exists() or sha256(pdf) != rec["render_sha256"]:
                return None
        else:
            pdf = VAULT / rel
            if not HASH_CHECK or not pdf.exists() or sha256(pdf) != rec.get("sha256"):
                return None
        return base64.b64decode(rm_ocr.render_page_b64(pdf, page, DPI, MAX_PX))
    except Exception as e:
        log.debug("gold snapshot for %s p%d unavailable: %s", rel, page, e)
        return None


def harvest_one(rel, rec, learned):
    """Learn from edits to one transcript. Returns the number of pages harvested."""
    out_rel = rec.get("out_path")
    if not out_rel:
        return 0
    side_path = rm_verify.sidecar_path(STATE, out_rel)
    side = rm_verify.load_json(side_path, None)
    out_md = _out_md_path(out_rel)
    if not side or not out_md.exists():
        return 0
    st = out_md.stat()
    if side.get("seen_mtime") == st.st_mtime:
        return 0
    data = out_md.read_bytes()
    sha = rm_verify.sha256_bytes(data)
    side["seen_mtime"] = st.st_mtime
    if sha == side.get("seen_sha"):
        rm_verify.save_json(side_path, side)
        return 0
    side["seen_sha"] = sha
    edits = rm_verify.edited_pages(side, rm_verify.parse_pages(data.decode("utf-8", "replace")))
    harvested = side.setdefault("harvested", {})
    new_terms = []
    for n, (written, current) in sorted(edits.items()):
        seen = {tuple(x) for x in harvested.get(str(n), [])}
        fresh = [c for c in rm_verify.diff_corrections(written, current)
                 if (c["before"], c["after"]) not in seen]
        if not fresh:
            continue
        with open(CORRECTIONS_LOG, "a") as f:
            for c in fresh:
                f.write(json.dumps({"at": datetime.datetime.now().isoformat(timespec="seconds"),
                                    "source": rel, "page": n, **c}, ensure_ascii=False) + "\n")
                for term in c["terms"]:
                    learned.record(term, f"{rel}#{n}")
                    new_terms.append(term)
        harvested[str(n)] = sorted(seen | {(c["before"], c["after"]) for c in fresh})
        rm_verify.write_gold_case(GOLDSET_DIR, rel, n, current, _page_png(rel, rec, n),
                                  source_sha256=rec.get("sha256"))
    rm_verify.save_json(side_path, side)
    if edits:
        log.info("learned from edits to %s: page(s) %s%s", out_md.name, sorted(edits),
                 f", terms {sorted(set(new_terms))}" if new_terms else "")
    return len(edits)


def harvest_edits(man):
    """Scan every transcript for edits made since the daemon wrote it."""
    if not LEARN_CORRECTIONS:
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    learned = rm_verify.LearnedVocab(LEARNED_VOCAB, LEARN_MIN_COUNT)
    n = 0
    for rel, rec in list(man.items()):
        if rec.get("status") == "ok":
            try:
                n += harvest_one(rel, rec, learned)
            except Exception as e:  # one unreadable transcript must not stop the rest
                log.warning("harvest failed for %s: %s", rel, e)
    if n:
        learned.save()
    return n


def gate_learned_terms():
    """Activate learned terms that have earned it, checked against the ground-truth set."""
    if not (LEARN_CORRECTIONS and USE_LEARNED_VOCAB):
        return
    learned = rm_verify.LearnedVocab(LEARNED_VOCAB, LEARN_MIN_COUNT)
    cand = learned.candidates()
    if not cand:
        return
    if not LEARN_GATE:
        learned.accept(cand)
        learned.save()
        log.info("learned vocab: activated %s (LEARN_GATE=0)", cand)
        return
    import rm_eval
    cases = rm_eval.load_goldset(GOLDSET_DIR)
    if not cases:
        log.info("learned vocab: %d candidate term(s) waiting for ground-truth pages to test on",
                 len(cand))
        return
    # Pages that actually contain the new terms say the most about them.
    lowered = [t.lower() for t in cand]
    cases.sort(key=lambda c: not any(t in c["truth"].lower() for t in lowered))
    cases = cases[:LEARN_GATE_PAGES]
    base = rm_verify.parse_terms(VOCAB_FILE.read_text()) if VOCAB_FILE.is_file() else []
    base += [t for t in learned.active() if t not in base]
    log.info("learned vocab: testing %s on %d page(s)", cand, len(cases))
    accepted, b, c = rm_eval.gate_terms(cases, MODEL, base, cand, timeout=TIMEOUT,
                                        num_ctx=NUM_CTX or 16384, log=log.debug)
    (learned.accept if accepted else learned.reject)(cand)
    learned.save()
    EVAL_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(EVAL_HISTORY, "a") as f:
        f.write(json.dumps({"at": datetime.datetime.now().isoformat(timespec="seconds"),
                            "kind": "learned-vocab-gate", "terms": cand, "accepted": accepted,
                            "base": b, "candidate": c}) + "\n")
    log.info("learned vocab: %s %s (CER %s -> %s)", "accepted" if accepted else "rejected",
             cand, b.get("primary_cer"), c.get("primary_cer"))


def process_one(src, result, rel, digest, man, page_regions=None):
    """OCR a single (rendered) PDF and write its manifest entry + transcript.

    ``src`` is the original input path (.pdf or bundle); ``result`` is the
    rm_render.RenderResult (``result.pdf`` is what OCR actually reads,
    ``result.title`` drives the transcript filename). For bundles, the manifest
    records ``render_sha256`` so we can trace which cached PDF produced this
    transcript (handy for a future --purge-orphans).

    ``page_regions`` is ``scan_once``'s (possibly nulled-out, see the
    AUTO_SPLIT note there) stroke-region hints — passed explicitly rather than
    read off ``result`` so the caller's alignment check is the only source of
    truth.
    """
    out_md = safe_output_path(src, result.title, source_sha256=result.source_sha256)
    log.info("processing %s", rel)
    st = src.stat()
    source_modified = _iso_mtime(st)             # last-modified of the source file
    hint = rm_verify.vocab_hint(current_terms())
    pages = ocr_pdf(result.pdf, MODEL, DPI, MAX_PX, timeout=TIMEOUT, threads=THREADS,
                    no_think=NO_THINK, skip_blank=SKIP_BLANK_PAGES, page_regions=page_regions,
                    reflow=REFLOW_PARAGRAPHS, num_ctx=NUM_CTX, prompt_extra=hint)
    verify = None
    if verify_applies(src):
        pages, verify = dual_read(result.pdf, pages, hint)
    stroke_regions_flagged = sum(
        rm_strokes.summarize(regions)["likely_drawing_regions"] for regions in page_regions
    ) if page_regions else None
    out_rel = str(out_md)
    for base in (OUT, VAULT):                     # prefer a tidy relative path
        try:
            out_rel = str(out_md.relative_to(base))
            break
        except ValueError:
            continue
    kept = 0
    model_pages = pages
    if LEARN_CORRECTIONS and out_md.exists():
        # Harvest any edits before this write replaces the file, then keep the
        # edits of every page whose new model output is unchanged.
        prev = man.get(rel, {})
        harvest_one(rel, {**prev, "out_path": prev.get("out_path") or out_rel},
                    rm_verify.LearnedVocab(LEARNED_VOCAB, LEARN_MIN_COUNT))
        side = rm_verify.load_json(rm_verify.sidecar_path(STATE, out_rel), None)
        if side:
            old_text = out_md.read_text()
            pages, kept, superseded = rm_verify.carry_over(
                pages, side, rm_verify.parse_pages(old_text))
            if superseded:
                keep = STATE / "superseded" / f"{out_md.stem}.{time.strftime('%Y%m%d-%H%M%S')}.md"
                keep.parent.mkdir(parents=True, exist_ok=True)
                keep.write_text(old_text)
                log.warning("%d edited page(s) of %s changed on the page itself and were "
                            "re-transcribed; the edited file is kept at %s",
                            superseded, out_md.name, keep)
    chars = write_md(out_md, result.title, rel, pages, source_modified=source_modified,
                     stroke_regions_flagged=stroke_regions_flagged, verify=verify, kept_edits=kept)
    if LEARN_CORRECTIONS:
        rm_verify.write_sidecar(STATE, out_rel, rel, pages, out_md.read_bytes(),
                                model_pages=model_pages)
    entry = {
        "mtime": st.st_mtime,
        "size": st.st_size,
        "sha256": digest,
        "source_modified": source_modified,
        "out_path": out_rel,
        "pages": len(pages),
        "chars_per_page": chars,
        "processed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "status": "ok",
        "retries": 0,
    }
    if result.rendered:
        entry["render_sha256"] = sha256(result.pdf)
    if verify:
        entry["verify"] = verify
    if kept:
        entry["kept_edits"] = kept
    man[rel] = entry
    save_manifest(man)
    log.info("ok %s -> %s (%dp, %d chars)%s", rel, out_md.name, len(pages), sum(chars),
             f", verify: {verify.get('flagged', 0)} flagged of {verify.get('spans', 0)} "
             f"disagreement(s)" if verify else "")
    if DAILY_NOTE_EMBED:
        try:  # an embed failure must never fail (or retry) a completed transcription
            embed_in_daily_note(out_md, result.title)
        except Exception as e:
            log.warning("daily-note embed failed for %s: %s", rel, e)


def scan_once(man):
    """A single incremental pass. Returns the number of files (re)processed."""
    if not SRC.is_dir():
        log.warning("source dir missing: %s", SRC)
        return 0
    cutoff = (time.time() - MAX_AGE_HOURS * 3600) if MAX_AGE_HOURS > 0 else None
    done = skipped_old = 0
    for src in rm_render.iter_inputs(SRC):
        if is_under_out(src):
            continue  # never transcribe files inside our own transcripts tree
        if cutoff is not None and src.stat().st_mtime < cutoff:
            skipped_old += 1
            continue  # outside the recency window (MAX_AGE_HOURS)
        try:
            rel = str(src.relative_to(VAULT))
        except ValueError:
            continue
        digest = needs_work(src, rel, man)
        if digest is False:
            continue
        # Render: passthrough for .pdf; rmc + pdfunite for .zip/.rmdoc/.rm;
        # a one-page wrap for .png/.jpg/.jpeg/.webp.
        # Cached under STATE/rendered, keyed by source bytes hash, so a re-
        # extracted-but-byte-identical bundle never re-renders.
        try:
            result = rm_render.render_to_pdf(src, cache_dir=STATE / "rendered",
                                             extract_regions=STROKE_CONTEXT)
        except Exception as e:
            rec = man.setdefault(rel, {})
            rec["status"] = "error"
            rec["error"] = f"render: {e}"
            rec["retries"] = rec.get("retries", 0) + 1
            st = src.stat()
            rec["mtime"], rec["size"] = st.st_mtime, st.st_size
            rec["sha256"] = digest if isinstance(digest, str) else rec.get("sha256")
            save_manifest(man)
            capped = " (retry cap reached)" if rec["retries"] >= MAX_RETRIES else ""
            log.error("err %s: render: %s [attempt %d]%s", rel, e, rec["retries"], capped)
            continue
        pdf_for_ocr = result.pdf
        page_regions = result.page_regions
        # AUTO_SPLIT: split the (tall) input in place first, then OCR the result.
        # For .pdf passthrough this rewrites the source — re-derive the change
        # token from the post-split bytes. For bundles, the bundle file is in
        # the read-only vault and untouched; only the cached render is split,
        # so the manifest's change token (bundle hash) stays valid as-is.
        if AUTO_SPLIT:
            try:
                cfg = SplitConfig(
                    min_aspect_ratio=SPLIT_MAX_ASPECT,
                    target_page_height=SPLIT_TARGET_PAGE_HEIGHT,
                    min_gap_height=SPLIT_MIN_GAP_HEIGHT,
                    whitespace_threshold=SPLIT_WHITESPACE_THRESHOLD,
                    max_segment_factor=SPLIT_MAX_SEGMENT_FACTOR,
                )
                did_split = split_in_place(pdf_for_ocr, cfg)
                if did_split:
                    # The stroke regions were computed per ORIGINAL .rm page; a
                    # whitespace-band re-split changes the final page count, so
                    # they'd no longer line up with the right page. Drop them
                    # rather than risk attaching a hint to the wrong page.
                    if page_regions is not None:
                        log.debug("stroke regions dropped for %s: AUTO_SPLIT changed page count", rel)
                    page_regions = None
                    if not result.rendered:
                        st = src.stat()
                        digest = sha256(src) if HASH_CHECK else f"mtime:{st.st_mtime}:{st.st_size}"
            except Exception as e:  # a split failure must not abort the batch
                rec = man.setdefault(rel, {})
                rec["status"] = "error"
                rec["error"] = f"auto-split: {e}"
                rec["retries"] = rec.get("retries", 0) + 1
                st = src.stat()
                rec["mtime"], rec["size"], rec["sha256"] = st.st_mtime, st.st_size, digest
                save_manifest(man)
                log.error("err %s: auto-split: %s [attempt %d]", rel, e, rec["retries"])
                continue
        # Page cap: refuse documents longer than MAX_PDF_PAGES. Applied to the
        # rendered, post-split PDF (what OCR would actually chew through), and
        # recorded with the count so the needs_work gate can re-queue it if the
        # cap is later raised or removed.
        if MAX_PDF_PAGES > 0:
            try:
                n_pages = _pdf_page_count(pdf_for_ocr)
            except Exception as e:
                n_pages = None  # count failure must not block OCR — let it try
                log.warning("page-count check failed for %s: %s (cap not applied)", rel, e)
            if n_pages is not None and n_pages > MAX_PDF_PAGES:
                st = src.stat()
                prev = man.get(rel, {}).get("status")
                man[rel] = {"mtime": st.st_mtime, "size": st.st_size, "sha256": digest,
                            "status": "skipped_pages", "page_count": n_pages,
                            "checked_at": datetime.datetime.now().isoformat(timespec="seconds")}
                save_manifest(man)
                if prev != "skipped_pages":  # log once on entering the state
                    log.info("skip %s: %d pages > MAX_PDF_PAGES=%d", rel, n_pages, MAX_PDF_PAGES)
                continue
        # Gate: don't OCR a PDF the splitter hasn't made readable yet. Cheap
        # (reads metadata + page boxes), far cheaper than an OCR run, and only
        # reached for new/changed files. Applied to the rendered PDF — what OCR
        # will actually see — not the bundle.
        if not split_ready(pdf_for_ocr, rel):
            st = src.stat()
            prev = man.get(rel, {}).get("status")
            man[rel] = {"mtime": st.st_mtime, "size": st.st_size, "sha256": digest,
                        "status": "pending_split",
                        "checked_at": datetime.datetime.now().isoformat(timespec="seconds")}
            save_manifest(man)
            if prev != "pending_split":  # log once on entering the state
                log.info("pending-split %s (too tall, awaiting splitter)", rel)
            continue
        # Record the attempt BEFORE OCR runs. ocr_pdf can die by SIGKILL (an OOM
        # on a big render) or a poppler segfault — a hard crash no try/except can
        # catch — so a caught exception is NOT the only way this step fails. If we
        # only bumped retries in the except handler, a hard crash would leave the
        # manifest untouched, the file would look brand-new on the next restart,
        # and the daemon would re-attempt the same poison file forever (never
        # reaching the retry cap, never reaching the files after it). Persisting an
        # incremented-retry "attempting" marker first makes the crash count, so a
        # genuinely un-OCR-able file is retry-capped and skipped like any other.
        st = src.stat()
        rec = man.setdefault(rel, {})
        rec.update(mtime=st.st_mtime, size=st.st_size,
                   sha256=digest if isinstance(digest, str) else rec.get("sha256"),
                   status="attempting", retries=rec.get("retries", 0) + 1,
                   attempted_at=datetime.datetime.now().isoformat(timespec="seconds"))
        save_manifest(man)
        try:
            process_one(src, result, rel, digest, man, page_regions=page_regions)
            done += 1
        except Exception as e:  # one bad input must not stop the batch
            rec = man.setdefault(rel, {})
            rec["status"] = "error"
            rec["error"] = str(e)
            rec["sha256"] = digest if isinstance(digest, str) else rec.get("sha256")
            # retries was already incremented by the pre-attempt marker above;
            # don't double-count a caught error against the cap.
            st = src.stat()
            rec["mtime"], rec["size"] = st.st_mtime, st.st_size
            save_manifest(man)
            capped = " (retry cap reached)" if rec.get("retries", 0) >= MAX_RETRIES else ""
            log.error("err %s: %s [attempt %d]%s", rel, e, rec.get("retries", 0), capped)
    if skipped_old:
        log.debug("skipped %d file(s) older than %sh", skipped_old, MAX_AGE_HOURS)
    return done


def print_learning_status(man):
    """Verification and learning at a glance: what is flagged, learned and measured."""
    v = Counter()
    docs = 0
    for r in man.values():
        if r.get("status") == "ok" and r.get("verify"):
            docs += 1
            v.update({k: n for k, n in r["verify"].items() if isinstance(n, int)})
    if docs:
        resolved = v["resolved_a"] + v["resolved_b"] + v["resolved_other"]
        print(f"verify: {docs} doc(s), {v['words']} words, {v['spans']} disagreement(s): "
              f"{resolved} resolved (A {v['resolved_a']}, B {v['resolved_b']}, "
              f"other {v['resolved_other']}), {v['flagged']} flagged for review")
    try:
        corrections = sum(1 for _ in open(CORRECTIONS_LOG))
    except OSError:
        corrections = 0
    lv = rm_verify.LearnedVocab(LEARNED_VOCAB, LEARN_MIN_COUNT)
    gold = list(GOLDSET_DIR.glob("*.json")) if GOLDSET_DIR.is_dir() else []
    if corrections or gold or lv.data["terms"]:
        print(f"learning: {corrections} correction(s) harvested, {len(gold)} ground-truth page(s), "
              f"learned terms active={len(lv.active())} candidates={len(lv.candidates())} "
              f"rejected={len(lv.data['rejected'])}")
    try:
        last = json.loads(EVAL_HISTORY.read_text().splitlines()[-1])
        print(f"last eval: {last.get('at')} {last.get('kind', 'run')} "
              f"accepted={last.get('accepted')} terms={last.get('terms')}")
    except (OSError, IndexError, ValueError):
        pass


def print_status(man):
    ok = sum(1 for r in man.values() if r.get("status") == "ok")
    err = sum(1 for r in man.values() if r.get("status") == "error")
    pending = sum(1 for r in man.values() if r.get("status") == "pending_split")
    # "attempting" persists only when OCR was entered but never finished — i.e. a
    # hard crash (OOM-kill / segfault) killed the process mid-file. Surfacing it
    # makes a wedged or retry-capped poison file visible instead of silent.
    attempting = sum(1 for r in man.values() if r.get("status") == "attempting")
    skipped = sum(1 for r in man.values() if r.get("status") == "skipped_pages")
    pages = sum(r.get("pages", 0) for r in man.values() if r.get("status") == "ok")
    print(f"manifest: {MANIFEST}")
    print(f"  ok={ok}  error={err}  pending_split={pending}  attempting={attempting}  "
          f"skipped_pages={skipped}  total_pages={pages}")
    print_learning_status(man)
    for rel, r in sorted(man.items()):
        if r.get("status") == "error":
            print(f"  ERROR    {rel}  (retries={r.get('retries', 0)}): {r.get('error', '')}")
        elif r.get("status") == "pending_split":
            print(f"  PENDING  {rel}  (awaiting splitter)")
        elif r.get("status") == "attempting":
            capped = " retry-capped" if r.get("retries", 0) >= MAX_RETRIES else ""
            print(f"  CRASHED  {rel}  (died mid-OCR, retries={r.get('retries', 0)}{capped})")
        elif r.get("status") == "skipped_pages":
            print(f"  SKIPPED  {rel}  ({r.get('page_count', '?')} pages > MAX_PDF_PAGES; "
                  f"raise the cap to re-queue)")


_INPUT_SUFFIX_TUPLE = tuple(sorted(rm_render.SUPPORTED_INPUT_SUFFIXES))


def start_inotify_watcher(src, wake):
    """Spawn a daemon thread that sets `wake` when a supported input file event fires under `src`.

    Recursive: walks the tree once, adds watches as new subdirs appear, drops
    watches on rmdir/mv. Best-effort — any failure here logs and returns without
    starting the thread, so the caller falls through to pure-poll behavior.

    A threading.Event is naturally idempotent, so a write-then-rename burst (50
    IN_MODIFY + 1 IN_CLOSE_WRITE + 1 IN_MOVED_TO) collapses to a single wake
    consumed on the main loop's next wait().
    """
    import threading
    try:
        from inotify_simple import INotify, flags
    except ImportError:
        log.warning("INOTIFY=1 but inotify_simple is not installed — falling back to pure poll")
        return None

    inotify = INotify()
    file_mask = flags.CLOSE_WRITE | flags.MOVED_TO
    dir_mask = file_mask | flags.CREATE | flags.MOVED_FROM | flags.MOVE_SELF | flags.DELETE_SELF
    wd_to_path = {}

    def add_dir(path):
        try:
            wd = inotify.add_watch(str(path), dir_mask)
            wd_to_path[wd] = path
        except OSError as e:
            log.warning("inotify add_watch %s: %s", path, e)

    add_dir(src)
    for dirpath, dirnames, _ in os.walk(src):
        for d in dirnames:
            add_dir(pathlib.Path(dirpath) / d)
    if not wd_to_path:
        log.warning("inotify: no watchable dirs under %s — falling back to pure poll", src)
        return None
    log.info("inotify watching %d dir(s) under %s", len(wd_to_path), src)

    def loop():
        while True:
            try:
                events = inotify.read()
            except Exception as e:
                log.exception("inotify read failed, watcher exiting: %s", e)
                return
            for ev in events:
                base = wd_to_path.get(ev.wd)
                if base is None:
                    continue
                if ev.mask & flags.IGNORED:
                    wd_to_path.pop(ev.wd, None)
                    continue
                # New subdir → watch it too.
                if ev.mask & flags.CREATE and ev.mask & flags.ISDIR and ev.name:
                    add_dir(base / ev.name)
                    continue
                # File-level event on a supported input → fire the wake. Derived
                # from rm_render rather than hardcoded, so a new input type can
                # never be silently poll-only.
                if ev.name and ev.name.lower().endswith(_INPUT_SUFFIX_TUPLE):
                    log.debug("inotify wake: %s/%s (mask=0x%x)", base, ev.name, ev.mask)
                    wake.set()

    t = threading.Thread(target=loop, name="rm-ocr-inotify", daemon=True)
    t.start()
    return t


def _probe_png(side):
    """A `side`x`side` PNG as raw bytes, built with the standard library only.

    Deliberately not Pillow. The gate has to be exercisable by selftest.py,
    which CI runs on a bare interpreter with no third-party packages
    installed — importing Pillow here is what made the self-test fail to even
    start. Encoding a solid image is a dozen lines, so the dependency buys
    nothing.

    A white field under a black bar: some encoders special-case a perfectly
    uniform image, and a real edge keeps the probe representative of a page.
    """
    import struct
    import zlib

    def chunk(tag, payload):
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    # 8-bit truecolour (colour type 2) — the most broadly accepted PNG flavour.
    ihdr = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    bar = max(1, side // 8)
    black, white = b"\x00" * (side * 3), b"\xff" * (side * 3)
    # Each scanline is prefixed with filter type 0 (None).
    raw = b"".join(b"\x00" + (black if y < bar else white) for y in range(side))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def assert_model_sees_images(host, model, min_image_tokens=64):
    """Refuse to start if `model` silently ignores the images we send it.

    This guards the worst failure this tool can have. Some Ollama runners accept
    an ``images=`` payload, drop it, and answer from the text prompt alone —
    measured on gemma4:12b-mlx under Ollama 0.32.0, which served a page of
    handwriting as a confident, fluent essay about 19th-century America and
    repeated it verbatim for every page. Nothing about that output looks wrong:
    it is well-formed prose under ``status: ok``. Silent fabrication in a
    journal is far worse than a visible failure.

    The signal is how prompt cost GROWS with image area, not the answer, so it
    does not depend on the model being any good at OCR — only on the image
    arriving. Comparing two sizes rather than image-vs-no-image matters twice
    over:

      * Token cost per image is wildly tokenizer-dependent. A 64x64 probe costs
        qwen3.5:9b 11 tokens and gemma4:26b 51 — both fully vision-capable, both
        of which an absolute "+200 tokens" threshold rejects as broken. Only
        qwen3-vl:8b's fixed-tile encoder charges ~1000 for a thumbnail.
      * Differencing two sizes cancels any constant. A runner that adds a fixed
        "image mode" preamble without encoding pixels cannot fake growth.

    Measured 64x64 -> 1024x1024 growth: qwen3.5:9b 30 -> 1045 (+1015),
    gemma4:26b 71 -> 278 (+207). A runner that drops images stays flat.
    """
    import urllib.request

    def prompt_tokens(images):
        body = {"model": model, "prompt": "What text is in this image?",
                "stream": False, "options": {"num_predict": 1}}
        if images:
            body["images"] = images
        req = urllib.request.Request(
            host + "/api/generate", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read()).get("prompt_eval_count") or 0

    def b64_png(side):
        return base64.b64encode(_probe_png(side)).decode()

    try:
        without = prompt_tokens(None)
        small = prompt_tokens([b64_png(64)])
        large = prompt_tokens([b64_png(1024)])
    except Exception as e:
        log.warning("vision check could not run (%s) — continuing unguarded", e)
        return
    growth = large - small
    log.info("vision check: %s prompt tokens %d none / %d at 64px / %d at 1024px "
             "(+%d for area)", model, without, small, large, growth)
    if growth < min_image_tokens:
        raise SystemExit(
            f"model {model!r} on {host} appears to IGNORE images: growing the "
            f"image from 64x64 to 1024x1024 changed the prompt from {small} to "
            f"{large} tokens (expected at least +{min_image_tokens}).\n"
            "It would answer from the prompt alone and write confident, "
            "entirely FABRICATED transcripts that look successful. Measured on "
            "gemma4:12b-mlx, whose MLX runner drops images silently.\n"
            "Use a model served by a vision-capable runner, or set "
            "VISION_CHECK=0 to skip this gate (not recommended).")


def wait_for_model(host, model, timeout):
    """Block until `model` is loadable on `host`, or raise SystemExit on timeout.

    Two stages, both via the same HTTP path rm_ocr.py uses, so DNS / port / model-name
    problems surface here instead of poisoning the manifest with instant 404s:
      1. presence — POST /api/show until 200 (404 = not pulled yet; URLError = ollama unreachable).
      2. smoke    — POST /api/generate (num_predict=1) once; proves the model actually loads.
    """
    import urllib.error
    import urllib.request

    show_url = host + "/api/show"
    gen_url = host + "/api/generate"
    show_body = json.dumps({"name": model}).encode()
    deadline = time.monotonic() + timeout
    delay = 2.0
    log.info("waiting for model %s on %s (timeout=%ds)", model, host, timeout)
    while True:
        try:
            req = urllib.request.Request(
                show_url, data=show_body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    break
        except urllib.error.HTTPError as e:
            log.info("ollama /api/show %d (%s) — waiting...", e.code,
                     "model not pulled yet" if e.code == 404 else e.reason)
        except urllib.error.URLError as e:
            log.warning("ollama unreachable at %s: %s — waiting...", show_url, e.reason)
        except Exception as e:
            log.warning("ollama probe error at %s: %s — waiting...", show_url, e)
        if time.monotonic() > deadline:
            raise SystemExit(
                f"timed out after {timeout}s waiting for model {model} on {host} "
                f"(set MODEL_WAIT_TIMEOUT=0 to disable this gate)")
        time.sleep(delay)
        delay = min(delay * 1.5, 30)

    log.info("model present; running smoke test (loads weights, may take a minute on CPU)")
    smoke_body = json.dumps({
        "model": model, "prompt": "ping", "stream": False,
        "options": {"num_predict": 1},
    }).encode()
    try:
        req = urllib.request.Request(
            gen_url, data=smoke_body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=max(timeout, 300)) as r:
            payload = json.loads(r.read())
    except Exception as e:
        raise SystemExit(f"smoke test failed for {model} on {host}: {e}")
    log.info("smoke test OK (sample=%r)", (payload.get("response") or "")[:40])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", action="store_true", help="Run a single incremental pass and exit")
    ap.add_argument("--status", action="store_true", help="Print manifest summary and exit")
    args = ap.parse_args()

    setup_logging()

    if args.status:
        print_status(load_manifest())
        return

    assert_safe_paths()
    rm_ocr.assert_local_host(rm_ocr.OLLAMA_URL, allow_remote=ALLOW_REMOTE_MODEL_HOST,
                             wait=max(MODEL_WAIT_TIMEOUT, 60), log=log.warning)
    if REQUIRE_SPLIT:
        try:
            import pypdf  # noqa: F401  fail fast if the split gate is on but pypdf is missing
        except ImportError:
            raise SystemExit("REQUIRE_SPLIT needs pypdf installed (pip install pypdf)")
    if AUTO_SPLIT:
        try:
            import fitz  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            raise SystemExit("AUTO_SPLIT needs PyMuPDF + numpy installed (pip install pymupdf numpy)")
    if STROKE_CONTEXT:
        try:
            import rmscene  # noqa: F401  normally already present transitively via rmc
        except ImportError:
            raise SystemExit("STROKE_CONTEXT needs rmscene installed (pip install rmscene)")
    log.info("rm-ocr starting | model=%s threads=%d no_think=%s dpi=%d max_px=%d max_age=%sh cooldown=%ss",
             MODEL, THREADS, NO_THINK, DPI, MAX_PX, MAX_AGE_HOURS, MIN_REPROCESS_INTERVAL)
    log.info("source=%s  out=%s  state=%s", SRC, OUT, STATE)
    if AUTO_SPLIT:
        log.info("AUTO_SPLIT ON | split in place then OCR (max_aspect=%.2f, target_h=%d)",
                 SPLIT_MAX_ASPECT, SPLIT_TARGET_PAGE_HEIGHT)
    if REQUIRE_SPLIT:
        log.info("split gate ON | marker=%s value=%s max_aspect=%.2f",
                 SPLIT_MARKER_KEY, SPLIT_MARKER_VALUE, SPLIT_MAX_ASPECT)
    if STROKE_CONTEXT:
        log.info("STROKE_CONTEXT ON | stroke-region hints for .rm-family sources (heuristic, not recognition)")
    if DAILY_NOTE_EMBED:
        log.info("DAILY_NOTE_EMBED ON | date-named transcripts embedded into %s/<date>.md (heading=%r)",
                 DAILY_NOTE_DIR, DAILY_NOTE_HEADING)
        if not (VAULT / DAILY_NOTE_DIR).is_dir():
            # The embed write would mkdir this path and "succeed" even when the
            # folder isn't a container mount — landing embeds in the ephemeral
            # layer where Obsidian never sees them. Loud warning, not fatal.
            log.warning("daily-note dir %s does not exist — in a container this usually means it "
                        "is not mounted; embeds would land in the ephemeral layer and be lost",
                        VAULT / DAILY_NOTE_DIR)
    if MAX_PDF_PAGES > 0:
        log.info("MAX_PDF_PAGES=%d | longer documents are skipped (visible via --status)", MAX_PDF_PAGES)
    terms = current_terms()
    if terms:
        log.info("vocabulary hint ON | %d term(s) from %s%s", len(terms), VOCAB_FILE,
                 " + learned" if USE_LEARNED_VOCAB else "")
    if VERIFY_MODEL:
        log.info("VERIFY ON | second reader=%s resolve=%s (model=%s) paths=%s unload=%s",
                 VERIFY_MODEL, VERIFY_RESOLVE, RESOLVE_MODEL,
                 ",".join(VERIFY_PATHS) or "all", VERIFY_UNLOAD)
    if LEARN_CORRECTIONS:
        log.info("LEARN_CORRECTIONS ON | edits harvested to %s, ground truth to %s, "
                 "learned vocab %s%s", CORRECTIONS_LOG, GOLDSET_DIR,
                 "in use" if USE_LEARNED_VOCAB else "collected only",
                 " (eval-gated)" if USE_LEARNED_VOCAB and LEARN_GATE else "")

    if MODEL_WAIT_TIMEOUT > 0:
        wait_for_model(OLLAMA_HOST, MODEL, MODEL_WAIT_TIMEOUT)
    else:
        log.info("MODEL_WAIT_TIMEOUT=0, skipping startup readiness gate")
    if VISION_CHECK:
        assert_model_sees_images(OLLAMA_HOST, MODEL, VISION_CHECK_MIN_TOKENS)
    if VERIFY_MODEL:
        # Same gates for the second reader and the resolver: a verify model that
        # drops images would "confirm" fabricated text.
        for extra in dict.fromkeys(m for m in (VERIFY_MODEL, RESOLVE_MODEL) if m != MODEL):
            if VERIFY_UNLOAD:
                _unload(MODEL)
            if MODEL_WAIT_TIMEOUT > 0:
                wait_for_model(OLLAMA_HOST, extra, MODEL_WAIT_TIMEOUT)
            if VISION_CHECK:
                assert_model_sees_images(OLLAMA_HOST, extra, VISION_CHECK_MIN_TOKENS)
            if VERIFY_UNLOAD:
                _unload(extra)
    if not VISION_CHECK:
        log.warning("VISION_CHECK=0 — not verifying the model actually receives "
                    "images; a runner that drops them writes fabricated transcripts")

    if args.scan:
        man = load_manifest()
        harvest_edits(man)
        n = scan_once(man)
        gate_learned_terms()
        log.info("scan complete: %d file(s) processed", n)
        return

    import threading
    wake = threading.Event()
    if INOTIFY:
        start_inotify_watcher(SRC, wake)

    while True:
        if in_run_window():
            try:
                man = load_manifest()
                harvest_edits(man)
                n = scan_once(man)
                if n:
                    log.info("pass complete: %d file(s) processed", n)
                gate_learned_terms()
            except Exception as e:
                log.exception("scan pass failed: %s", e)
        else:
            log.info("outside RUN_WINDOW=%s, sleeping", RUN_WINDOW)
        wake.wait(INTERVAL)
        wake.clear()


if __name__ == "__main__":
    main()
