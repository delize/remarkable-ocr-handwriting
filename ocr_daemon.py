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
import datetime
import hashlib
import json
import logging
import os
import pathlib
import sys
import time

import rm_render
from rm_ocr import _safe, ocr_pdf  # reuse the proven core
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
THREADS = int(os.environ.get("THREADS", "14"))
NO_THINK = _env_bool("NO_THINK", True)
# Skip the OCR call entirely for a page that's genuinely blank (rm_ocr's cheap
# pixel-stat pre-check). Small local vision models tend to answer a blank page
# with refusal-style prose instead of nothing, which pollutes the transcript —
# on by default since there's nothing useful to lose by skipping.
SKIP_BLANK_PAGES = _env_bool("SKIP_BLANK_PAGES", True)
DPI = int(os.environ.get("DPI", "150"))
MAX_PX = int(os.environ.get("MAX_PX", "1568"))
TIMEOUT = int(os.environ.get("TIMEOUT", "1800"))
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
# dir to be WRITABLE (not the usual :ro vault mount) and pulls in pypdf + Pillow +
# numpy. Implies split-readiness, so REQUIRE_SPLIT's gate is moot when this is on.
AUTO_SPLIT = _env_bool("AUTO_SPLIT", False)
SPLIT_TARGET_PAGE_HEIGHT = int(os.environ.get("SPLIT_TARGET_PAGE_HEIGHT", "700"))
SPLIT_MIN_GAP_HEIGHT = int(os.environ.get("SPLIT_MIN_GAP_HEIGHT", "25"))
SPLIT_WHITESPACE_THRESHOLD = int(os.environ.get("SPLIT_WHITESPACE_THRESHOLD", "248"))

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


def write_md(out_md, title, rel, pages, source_modified=None):
    out_md.parent.mkdir(parents=True, exist_ok=True)
    chars = [len(text) for _, text in pages]
    fm = [
        "---",
        f"source: {rel}",
        f"model: {MODEL}",
        f"source_modified: {source_modified}" if source_modified else None,
        f"processed_at: {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"pages: {len(pages)}",
        f"chars_per_page: {json.dumps(chars)}",
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


def process_one(src, result, rel, digest, man):
    """OCR a single (rendered) PDF and write its manifest entry + transcript.

    ``src`` is the original input path (.pdf or bundle); ``result`` is the
    rm_render.RenderResult (``result.pdf`` is what OCR actually reads,
    ``result.title`` drives the transcript filename). For bundles, the manifest
    records ``render_sha256`` so we can trace which cached PDF produced this
    transcript (handy for a future --purge-orphans).
    """
    out_md = safe_output_path(src, result.title, source_sha256=result.source_sha256)
    log.info("processing %s", rel)
    st = src.stat()
    source_modified = _iso_mtime(st)             # last-modified of the source file
    pages = ocr_pdf(result.pdf, MODEL, DPI, MAX_PX, timeout=TIMEOUT, threads=THREADS,
                    no_think=NO_THINK, skip_blank=SKIP_BLANK_PAGES)
    chars = write_md(out_md, result.title, rel, pages, source_modified=source_modified)
    out_rel = str(out_md)
    for base in (OUT, VAULT):                     # prefer a tidy relative path
        try:
            out_rel = str(out_md.relative_to(base))
            break
        except ValueError:
            continue
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
    man[rel] = entry
    save_manifest(man)
    log.info("ok %s -> %s (%dp, %d chars)", rel, out_md.name, len(pages), sum(chars))


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
        # Render: passthrough for .pdf; rmc + pdfunite for .zip/.rmdoc/.rm.
        # Cached under STATE/rendered, keyed by source bytes hash, so a re-
        # extracted-but-byte-identical bundle never re-renders.
        try:
            result = rm_render.render_to_pdf(src, cache_dir=STATE / "rendered")
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
                )
                if split_in_place(pdf_for_ocr, cfg) and not result.rendered:
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
        try:
            process_one(src, result, rel, digest, man)
            done += 1
        except Exception as e:  # one bad input must not stop the batch
            rec = man.setdefault(rel, {})
            rec["status"] = "error"
            rec["error"] = str(e)
            rec["sha256"] = digest if isinstance(digest, str) else rec.get("sha256")
            rec["retries"] = rec.get("retries", 0) + 1
            st = src.stat()
            rec["mtime"], rec["size"] = st.st_mtime, st.st_size
            save_manifest(man)
            capped = " (retry cap reached)" if rec["retries"] >= MAX_RETRIES else ""
            log.error("err %s: %s [attempt %d]%s", rel, e, rec["retries"], capped)
    if skipped_old:
        log.debug("skipped %d file(s) older than %sh", skipped_old, MAX_AGE_HOURS)
    return done


def print_status(man):
    ok = sum(1 for r in man.values() if r.get("status") == "ok")
    err = sum(1 for r in man.values() if r.get("status") == "error")
    pending = sum(1 for r in man.values() if r.get("status") == "pending_split")
    pages = sum(r.get("pages", 0) for r in man.values() if r.get("status") == "ok")
    print(f"manifest: {MANIFEST}")
    print(f"  ok={ok}  error={err}  pending_split={pending}  total_pages={pages}")
    for rel, r in sorted(man.items()):
        if r.get("status") == "error":
            print(f"  ERROR    {rel}  (retries={r.get('retries', 0)}): {r.get('error', '')}")
        elif r.get("status") == "pending_split":
            print(f"  PENDING  {rel}  (awaiting splitter)")


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
                # File-level event on a *.pdf → fire the wake.
                if ev.name and ev.name.lower().endswith((".pdf", ".zip", ".rmdoc", ".rm")):
                    log.debug("inotify wake: %s/%s (mask=0x%x)", base, ev.name, ev.mask)
                    wake.set()

    t = threading.Thread(target=loop, name="rm-ocr-inotify", daemon=True)
    t.start()
    return t


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
    if REQUIRE_SPLIT or AUTO_SPLIT:
        try:
            import pypdf  # noqa: F401  fail fast if a split feature is on but pypdf is missing
        except ImportError:
            raise SystemExit("REQUIRE_SPLIT/AUTO_SPLIT need pypdf installed (pip install pypdf)")
    if AUTO_SPLIT:
        try:
            import PIL  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            raise SystemExit("AUTO_SPLIT needs Pillow + numpy installed (pip install pillow numpy)")
    log.info("rm-ocr starting | model=%s threads=%d no_think=%s dpi=%d max_px=%d max_age=%sh cooldown=%ss",
             MODEL, THREADS, NO_THINK, DPI, MAX_PX, MAX_AGE_HOURS, MIN_REPROCESS_INTERVAL)
    log.info("source=%s  out=%s  state=%s", SRC, OUT, STATE)
    if AUTO_SPLIT:
        log.info("AUTO_SPLIT ON | split in place then OCR (max_aspect=%.2f, target_h=%d)",
                 SPLIT_MAX_ASPECT, SPLIT_TARGET_PAGE_HEIGHT)
    if REQUIRE_SPLIT:
        log.info("split gate ON | marker=%s value=%s max_aspect=%.2f",
                 SPLIT_MARKER_KEY, SPLIT_MARKER_VALUE, SPLIT_MAX_ASPECT)

    if MODEL_WAIT_TIMEOUT > 0:
        wait_for_model(OLLAMA_HOST, MODEL, MODEL_WAIT_TIMEOUT)
    else:
        log.info("MODEL_WAIT_TIMEOUT=0, skipping startup readiness gate")

    if args.scan:
        n = scan_once(load_manifest())
        log.info("scan complete: %d file(s) processed", n)
        return

    import threading
    wake = threading.Event()
    if INOTIFY:
        start_inotify_watcher(SRC, wake)

    while True:
        if in_run_window():
            try:
                n = scan_once(load_manifest())
                if n:
                    log.info("pass complete: %d file(s) processed", n)
            except Exception as e:
                log.exception("scan pass failed: %s", e)
        else:
            log.info("outside RUN_WINDOW=%s, sleeping", RUN_WINDOW)
        wake.wait(INTERVAL)
        wake.clear()


if __name__ == "__main__":
    main()
