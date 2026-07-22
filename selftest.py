#!/usr/bin/env python3
"""Offline self-test for the daemon logic — no Ollama, no poppler, no real PDFs.

Stubs pdf2image and rm_ocr.ocr_pdf so we exercise the scanner, manifest,
change-detection, transcript writer, path guards and error handling in isolation.

    python3 selftest.py        # prints PASS/FAIL for each behavior, exits non-zero on failure
"""
import os
import sys
import time
import types
import tempfile
import pathlib


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rmocr-selftest-"))
    for d in ["vault/remarkable/Work", "out", "state"]:
        (tmp / d).mkdir(parents=True, exist_ok=True)
    (tmp / "vault/remarkable/Work/Sample.pdf").write_text("pdf-bytes-v1")
    (tmp / "vault/remarkable/Work/Bad.pdf").write_text("broken")

    os.environ.update(
        VAULT_DIR=str(tmp / "vault"),
        SOURCE_SUBDIR="remarkable",
        OUT_DIR=str(tmp / "out"),          # output base OUTSIDE the (read-only) vault
        STATE_DIR=str(tmp / "state"),
        MODEL="gemma4:26b",
    )
    out_base = tmp / "out"

    # stub pdf2image so importing rm_ocr needs no poppler
    m = types.ModuleType("pdf2image")
    m.convert_from_path = lambda *a, **k: []
    m.pdfinfo_from_path = lambda *a, **k: {"Pages": 0}
    sys.modules["pdf2image"] = m

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import rm_ocr

    _real_ocr_pdf = rm_ocr.ocr_pdf  # keep a handle before stubbing, for the blank-page checks below

    def fake_ocr(pdf, *a, **k):
        if pathlib.Path(pdf).stem == "Bad":
            raise RuntimeError("simulated bad PDF")
        return [(1, "Hello world\nline two"), (2, "page two text")]

    rm_ocr.ocr_pdf = fake_ocr
    import ocr_daemon
    ocr_daemon.ocr_pdf = fake_ocr

    # Stub the renderer so bundle/.rm tests don't need rmc, rmscene, or real
    # .rm bytes. The stub mirrors rm_render.render_to_pdf's contract: .pdf is
    # passthrough; bundles/.rm produce a fake PDF in cache_dir (or workdir).
    # RENDER_TITLES overrides the title per source filename (mimics visibleName);
    # RENDER_FAILS makes a source raise to exercise the error path.
    import rm_render
    RENDER_TITLES = {}
    RENDER_FAILS = set()
    RENDER_REGIONS = {}  # filename -> page_regions, for STROKE_CONTEXT tests

    def fake_render(src, *, cache_dir=None, workdir=None, extract_regions=False):
        src = pathlib.Path(src)
        suffix = src.suffix.lower()
        if suffix not in rm_render.SUPPORTED_INPUT_SUFFIXES:
            raise ValueError(f"unsupported: {suffix}")
        sha = rm_render._sha256_file(src)
        if suffix == ".pdf":
            return rm_render.RenderResult(pdf=src, title=src.stem, rendered=False,
                                          source_sha256=sha)
        if src.name in RENDER_FAILS:
            raise RuntimeError("simulated render failure")
        title = RENDER_TITLES.get(src.name, src.stem)
        page_regions = RENDER_REGIONS.get(src.name) if extract_regions else None
        if cache_dir is not None:
            out = rm_render._cache_path(cache_dir, sha)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"%PDF-1.4\nfake-rendered-bytes")
        elif workdir is not None:
            workdir = pathlib.Path(workdir)
            workdir.mkdir(parents=True, exist_ok=True)
            out = workdir / f"{sha[:16]}.pdf"
            out.write_bytes(b"%PDF-1.4\nfake-rendered-bytes")
        else:
            raise ValueError("need cache_dir or workdir")
        return rm_render.RenderResult(pdf=out, title=title, rendered=True,
                                      source_sha256=sha, page_regions=page_regions)

    rm_render.render_to_pdf = fake_render

    ocr_daemon.setup_logging()
    ocr_daemon.assert_safe_paths()

    failures = []

    def check(name, got, want):
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
        if not ok:
            failures.append(name)

    # gate decisions are reachable without OCR: prefilter-skip happens before any
    # hash; queued is the only path that returns a token (and thus runs OCR).
    import logging as _logging
    gate_msgs = []

    class _Capture(_logging.Handler):
        def emit(self, r):
            gate_msgs.append(r.getMessage())

    ocr_daemon.log.addHandler(_Capture())
    ocr_daemon.log.setLevel(_logging.DEBUG)

    check("pass1 processes new files (Sample ok, Bad errors)",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    check("pass1 logs a queued gate (file changed -> OCR)",
          any("gate=queued" in m for m in gate_msgs), True)
    gate_msgs.clear()
    check("pass2 idempotent",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 0)
    check("pass2 skips via prefilter (no hash, no OCR)",
          any("gate=prefilter-skip" in m for m in gate_msgs), True)

    (tmp / "vault/remarkable/Work/Sample.pdf").write_text("pdf-bytes-v1")  # same bytes, new mtime
    check("touch-only change skipped",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 0)

    (tmp / "vault/remarkable/Work/Sample.pdf").write_text("pdf-bytes-v2")  # real edit
    check("real edit reprocesses one file",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)

    man = ocr_daemon.load_manifest()
    bad = man["remarkable/Work/Bad.pdf"]
    check("bad pdf recorded as error", bad["status"], "error")
    check("bad pdf retry counter increments", bad["retries"] >= 1, True)

    # recency window: a file older than MAX_AGE_HOURS is never even considered
    old = tmp / "vault/remarkable/Work/Old.pdf"
    old.write_text("old-bytes")
    backdate = time.time() - 48 * 3600
    os.utime(old, (backdate, backdate))
    saved_age = ocr_daemon.MAX_AGE_HOURS
    ocr_daemon.MAX_AGE_HOURS = 24
    check("file older than recency window is skipped",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 0)
    check("no transcript for out-of-window file",
          (out_base / "Work/Old-handwriting_converted.md").exists(), False)
    ocr_daemon.MAX_AGE_HOURS = 0  # disable window -> backfill the old file
    check("MAX_AGE_HOURS=0 backfills the old file",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    ocr_daemon.MAX_AGE_HOURS = saved_age

    # cooldown: a byte-changed file reprocessed within the interval is held off
    saved_cd = ocr_daemon.MIN_REPROCESS_INTERVAL
    ocr_daemon.MIN_REPROCESS_INTERVAL = 3600
    (tmp / "vault/remarkable/Work/Sample.pdf").write_text("pdf-bytes-v3-rapid-edit")
    check("cooldown suppresses rapid reprocess",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 0)
    ocr_daemon.MIN_REPROCESS_INTERVAL = saved_cd

    # is_under_out still guards the legacy in-vault layout
    saved_out = ocr_daemon.OUT
    ocr_daemon.OUT = tmp / "vault/remarkable/_transcripts"
    inside = tmp / "vault/remarkable/_transcripts/x.pdf"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("x")
    check("is_under_out excludes in-vault transcripts (legacy mode)",
          ocr_daemon.is_under_out(inside), True)
    ocr_daemon.OUT = saved_out

    sample_md = out_base / "Work/Sample-handwriting_converted.md"
    check("filename uses OUT_SUFFIX",
          ocr_daemon.safe_output_path(tmp / "vault/remarkable/Work/Sample.pdf").name,
          "Sample-handwriting_converted.md")
    check("transcript written to OUT_DIR base with suffix", sample_md.exists(), True)
    md = sample_md.read_text()
    check("transcript has frontmatter source", "source: remarkable/Work/Sample.pdf" in md, True)
    check("transcript records source last-modified", "source_modified:" in md, True)
    check("manifest records source last-modified",
          "source_modified" in ocr_daemon.load_manifest()["remarkable/Work/Sample.pdf"], True)
    check("transcript has backlink", "Source: [[remarkable/Work/Sample.pdf]]" in md, True)
    check("transcript has per-page bodies", "## Page 1" in md and "## Page 2" in md, True)
    check("transcript not written for failed pdf",
          (out_base / "Work/Bad-handwriting_converted.md").exists(), False)

    # alongside mode: transcript lands in the source PDF's own folder
    saved_al = ocr_daemon.OUT_ALONGSIDE
    ocr_daemon.OUT_ALONGSIDE = True
    op = ocr_daemon.safe_output_path(tmp / "vault/remarkable/Work/Sample.pdf")
    check("alongside mode writes next to source",
          op == tmp / "vault/remarkable/Work/Sample-handwriting_converted.md", True)
    ocr_daemon.OUT_ALONGSIDE = saved_al

    # empty suffix + alongside is refused (could clobber a Scrybble stub)
    saved_sfx = ocr_daemon.OUT_SUFFIX
    ocr_daemon.OUT_ALONGSIDE = True
    ocr_daemon.OUT_SUFFIX = ""
    try:
        ocr_daemon.assert_safe_paths()
        check("alongside+empty-suffix refused", False, True)
    except SystemExit:
        check("alongside+empty-suffix refused", True, True)
    finally:
        ocr_daemon.OUT_ALONGSIDE = saved_al
        ocr_daemon.OUT_SUFFIX = saved_sfx

    # --- split-readiness gate (REQUIRE_SPLIT) ---
    # Stub _pdf_split_info so we don't need pypdf or a real PDF: map filename ->
    # (marker, max_aspect). Tall.pdf is un-split, Short.pdf never needed it,
    # Marked.pdf carries the marker.
    split_info = {
        "Tall.pdf": (None, 7.8),
        "Short.pdf": (None, 1.3),
        "Marked.pdf": ("processed", 7.8),
    }
    ocr_daemon._pdf_split_info = lambda pdf: split_info[pathlib.Path(pdf).name]
    for name in split_info:
        (tmp / "vault/remarkable/Work" / name).write_text(f"bytes-{name}")

    ocr_daemon.REQUIRE_SPLIT = True
    ocr_daemon.SPLIT_MAX_ASPECT = 2.0
    gate_msgs.clear()
    ocr_daemon.scan_once(ocr_daemon.load_manifest())
    man = ocr_daemon.load_manifest()
    check("gate: tall un-split PDF held as pending_split",
          man["remarkable/Work/Tall.pdf"]["status"], "pending_split")
    check("gate: short PDF (never needed split) is OCR'd",
          man["remarkable/Work/Short.pdf"]["status"], "ok")
    check("gate: marked PDF is OCR'd",
          man["remarkable/Work/Marked.pdf"]["status"], "ok")
    check("gate: pending logged once on entry",
          sum("Tall.pdf (too tall, awaiting splitter)" in m for m in gate_msgs), 1)

    # Next pass with unchanged bytes: pending file is skipped silently (not re-logged).
    gate_msgs.clear()
    ocr_daemon.scan_once(ocr_daemon.load_manifest())
    check("gate: unchanged pending file not re-logged",
          any("Tall.pdf (too tall, awaiting splitter)" in m for m in gate_msgs), False)

    # Splitter runs: file changes + now reports the marker -> gets OCR'd.
    split_info["Tall.pdf"] = ("processed", 7.8)
    (tmp / "vault/remarkable/Work/Tall.pdf").write_text("bytes-Tall-split")
    ocr_daemon.scan_once(ocr_daemon.load_manifest())
    check("gate: file transcribed after splitter marks it",
          ocr_daemon.load_manifest()["remarkable/Work/Tall.pdf"]["status"], "ok")
    ocr_daemon.REQUIRE_SPLIT = False

    # --- bundle / loose-.rm dispatch ---
    gate_msgs.clear()

    # .zip dispatch — stub assigns the visibleName-derived title via RENDER_TITLES.
    (tmp / "vault/remarkable/Work/Bundle.zip").write_bytes(b"PK\x03\x04bundle-bytes-v1")
    RENDER_TITLES["Bundle.zip"] = "Bundle Notes"
    check(".zip dispatch processes one file",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    zip_entry = ocr_daemon.load_manifest()["remarkable/Work/Bundle.zip"]
    check(".zip manifest entry records render_sha256", "render_sha256" in zip_entry, True)
    check(".zip transcript uses visibleName-derived filename",
          (out_base / "Work/Bundle Notes-handwriting_converted.md").exists(), True)
    check(".zip pass2 idempotent",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 0)

    # .rmdoc dispatch — proves .zip and .rmdoc share the bundle path.
    (tmp / "vault/remarkable/Work/Modern.rmdoc").write_bytes(b"PK\x03\x04rmdoc-bytes-v1")
    RENDER_TITLES["Modern.rmdoc"] = "Modern Doc"
    check(".rmdoc dispatch processes one file",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    check(".rmdoc transcript named from visibleName",
          (out_base / "Work/Modern Doc-handwriting_converted.md").exists(), True)

    # loose .rm — single-file render path; no .metadata, title falls back to stem.
    (tmp / "vault/remarkable/Work/Stray.rm").write_bytes(b"rm-bytes")
    check("loose .rm processes one file",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    check("loose .rm transcript named from stem",
          (out_base / "Work/Stray-handwriting_converted.md").exists(), True)

    # Title precedence: a uuid-named bundle gets the friendly visibleName title.
    uuid_name = "9c4f1234-5678.rmdoc"
    (tmp / "vault/remarkable/Work" / uuid_name).write_bytes(b"PK\x03\x04uuid-bundle")
    RENDER_TITLES[uuid_name] = "Real Name"
    check("uuid bundle is queued and processed",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    check("uuid bundle transcript uses friendly title, not uuid",
          (out_base / "Work/Real Name-handwriting_converted.md").exists(), True)
    check("uuid bundle did NOT write a uuid-named transcript",
          (out_base / "Work/9c4f1234-5678-handwriting_converted.md").exists(), False)

    # Title fallback: no RENDER_TITLES entry → stub uses stem; safe_output_path
    # safe-ifies it (no special chars here, passes through verbatim).
    (tmp / "vault/remarkable/Work/Fallback.rmdoc").write_bytes(b"PK\x03\x04fallback")
    check("fallback bundle processes one file",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    check("fallback transcript uses stem-derived filename",
          (out_base / "Work/Fallback-handwriting_converted.md").exists(), True)

    # Bundle bytes change → reprocess. Note: the OCR step runs because the bundle
    # hash changed, even though the FAKE render cache writes the same bytes; that
    # mirrors production where the change-detection key is the source, not the cache.
    (tmp / "vault/remarkable/Work/Bundle.zip").write_bytes(b"PK\x03\x04bundle-bytes-v2")
    check("bundle bytes change reprocesses",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)

    # Render failure → manifest records status=error with the render: prefix.
    (tmp / "vault/remarkable/Work/Broken.rmdoc").write_bytes(b"PK\x03\x04broken")
    RENDER_FAILS.add("Broken.rmdoc")
    ocr_daemon.scan_once(ocr_daemon.load_manifest())
    broken = ocr_daemon.load_manifest()["remarkable/Work/Broken.rmdoc"]
    check("render failure recorded as status=error", broken["status"], "error")
    check("render failure error message starts with 'render:'",
          broken["error"].startswith("render:"), True)
    check("render failure retries=1", broken["retries"], 1)

    # --- crash-guard: a persistently un-OCR-able file is retry-capped, not looped ---
    # Stands in for the production OOM/segfault case. The pre-attempt "attempting"
    # marker makes every failed OCR attempt count, so MAX_RETRIES eventually skips
    # the file instead of re-attempting it on every pass forever; and a good file
    # that sorts AFTER the failing one must still be transcribed each pass.
    saved_ocr = ocr_daemon.ocr_pdf

    def crash_ocr(pdf, *a, **k):
        if pathlib.Path(pdf).stem == "Crashy":
            raise RuntimeError("simulated hard OCR failure")
        return [(1, "good text")]

    ocr_daemon.ocr_pdf = crash_ocr
    (tmp / "vault/remarkable/Work/Crashy.pdf").write_text("crashy-bytes")
    (tmp / "vault/remarkable/Work/Zzz.pdf").write_text("zzz-good-bytes")  # sorts after Crashy
    for _ in range(ocr_daemon.MAX_RETRIES + 2):
        ocr_daemon.scan_once(ocr_daemon.load_manifest())
    crash_man = ocr_daemon.load_manifest()
    check("crash-guard: failing file retry-capped exactly at MAX_RETRIES (no runaway)",
          crash_man["remarkable/Work/Crashy.pdf"]["retries"], ocr_daemon.MAX_RETRIES)
    check("crash-guard: good file sorted after the failing one is still transcribed",
          crash_man["remarkable/Work/Zzz.pdf"]["status"], "ok")
    ocr_daemon.ocr_pdf = saved_ocr

    # --- DAILY_NOTE_EMBED: transcripts of date-named sources land in daily notes ---
    saved_dne, saved_out_dne = ocr_daemon.DAILY_NOTE_EMBED, ocr_daemon.OUT
    ocr_daemon.DAILY_NOTE_EMBED = True
    ocr_daemon.OUT = tmp / "vault/transcripts"  # embeds require transcripts INSIDE the vault
    daily_dir = tmp / "vault/Daily Journal"
    daily_dir.mkdir(parents=True, exist_ok=True)

    # New note: a date-named source creates the daily note with the embed.
    (tmp / "vault/remarkable/Work/2026-07-20.pdf").write_text("journal-bytes-v1")
    check("daily-note embed: date-named source is processed",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    note = daily_dir / "2026-07-20.md"
    embed_line = "![[transcripts/Work/2026-07-20-handwriting_converted]]"
    check("daily-note embed: missing note is created", note.exists(), True)
    check("daily-note embed: note embeds the transcript by full vault path",
          embed_line in note.read_text(), True)

    # Existing note: prose is preserved, section appended, exactly once.
    note2 = daily_dir / "2026-07-21.md"
    note2.write_text("morning thoughts\n\nmore prose")
    (tmp / "vault/remarkable/Work/2026-07-21.pdf").write_text("journal-bytes-v1")
    ocr_daemon.scan_once(ocr_daemon.load_manifest())
    check("daily-note embed: existing prose preserved",
          note2.read_text().startswith("morning thoughts\n\nmore prose"), True)
    check("daily-note embed: section appended to existing note",
          "## reMarkable journal" in note2.read_text(), True)

    # Idempotent: re-OCR after a byte change must not duplicate the embed.
    (tmp / "vault/remarkable/Work/2026-07-21.pdf").write_text("journal-bytes-v2")
    ocr_daemon.scan_once(ocr_daemon.load_manifest())
    check("daily-note embed: re-OCR does not duplicate the section",
          note2.read_text().count("2026-07-21-handwriting_converted"), 1)

    # Non-date sources never touch daily notes.
    (tmp / "vault/remarkable/Work/NotADate.pdf").write_text("misc-bytes")
    ocr_daemon.scan_once(ocr_daemon.load_manifest())
    check("daily-note embed: non-date source creates no note",
          (daily_dir / "NotADate.md").exists(), False)

    # Transcripts outside the vault can't be transcluded: skip, never crash.
    ocr_daemon.OUT = saved_out_dne  # back to the outside-the-vault OUT_DIR
    (tmp / "vault/remarkable/Work/2026-07-22.pdf").write_text("journal-bytes-v1")
    check("daily-note embed: outside-vault OUT still processes the file",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    check("daily-note embed: outside-vault OUT writes no note",
          (daily_dir / "2026-07-22.md").exists(), False)
    ocr_daemon.DAILY_NOTE_EMBED = saved_dne

    # Config guard: a daily-note dir inside the source tree is refused at startup.
    saved_dnd = ocr_daemon.DAILY_NOTE_DIR
    ocr_daemon.DAILY_NOTE_EMBED, ocr_daemon.DAILY_NOTE_DIR = True, "remarkable/Daily Journal"
    try:
        ocr_daemon.assert_safe_paths()
        check("daily-note embed: DAILY_NOTE_DIR inside SOURCE_SUBDIR is refused", "no exit", "SystemExit")
    except SystemExit:
        check("daily-note embed: DAILY_NOTE_DIR inside SOURCE_SUBDIR is refused", "SystemExit", "SystemExit")
    ocr_daemon.DAILY_NOTE_EMBED, ocr_daemon.DAILY_NOTE_DIR = saved_dne, saved_dnd

    # --- MAX_PDF_PAGES: over-cap documents are skipped, and re-queue when the cap lifts ---
    saved_cap = ocr_daemon.MAX_PDF_PAGES
    saved_count = ocr_daemon._pdf_page_count
    ocr_daemon.MAX_PDF_PAGES = 10
    ocr_daemon._pdf_page_count = lambda p: 966 if "Tome" in str(p) else 2
    (tmp / "vault/remarkable/Work/Tome.pdf").write_text("tome-bytes")
    (tmp / "vault/remarkable/Work/Slim.pdf").write_text("slim-bytes")
    check("page cap: only the under-cap file is OCR'd",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    tome = ocr_daemon.load_manifest()["remarkable/Work/Tome.pdf"]
    check("page cap: over-cap file recorded as skipped_pages", tome["status"], "skipped_pages")
    check("page cap: recorded page count", tome["page_count"], 966)
    check("page cap: skipped file stays skipped next pass (no rework)",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 0)
    ocr_daemon.MAX_PDF_PAGES = 1000  # cap raised -> re-queues with no touch
    check("page cap: raising the cap re-queues the skipped file",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    check("page cap: re-queued file transcribed ok",
          ocr_daemon.load_manifest()["remarkable/Work/Tome.pdf"]["status"], "ok")
    ocr_daemon.MAX_PDF_PAGES = saved_cap
    ocr_daemon._pdf_page_count = saved_count

    # --- rm_strokes unit checks (pure logic, no real .rm bytes needed) ---
    import rm_strokes

    class _FakeTool:
        def __init__(self, name):
            self.name = name

    class _FakePoint:
        def __init__(self, x, y):
            self.x = x
            self.y = y

    class _FakeLine:
        def __init__(self, points, tool):
            self.points = [_FakePoint(x, y) for x, y in points]
            self.tool = _FakeTool(tool)

    check("regions_from_lines: no strokes -> no regions",
          rm_strokes.regions_from_lines([]), [])

    # Two short, adjacent, thin horizontal strokes -> one merged, text-like region.
    text_lines = [
        _FakeLine([(0, 0), (40, 2)], "FINELINER_1"),
        _FakeLine([(45, 1), (90, 3)], "FINELINER_1"),
    ]
    text_regions = rm_strokes.regions_from_lines(text_lines)
    check("regions_from_lines: adjacent thin strokes merge into one region",
          len(text_regions), 1)
    check("regions_from_lines: thin horizontal region is not flagged as a drawing",
          text_regions[0].likely_drawing, False)
    check("prompt_hint: no drawing regions -> None",
          rm_strokes.prompt_hint([r.__dict__ for r in text_regions]), None)

    # A tall, roughly-square cluster of strokes -> flagged as a probable drawing.
    drawing_lines = [
        _FakeLine([(0, 0), (200, 200)], "PAINTBRUSH_1"),
        _FakeLine([(0, 200), (200, 0)], "PAINTBRUSH_1"),
    ]
    drawing_regions = rm_strokes.regions_from_lines(drawing_lines)
    check("regions_from_lines: tall/squarish cluster flagged likely_drawing",
          drawing_regions[0].likely_drawing, True)
    hint = rm_strokes.prompt_hint([r.__dict__ for r in drawing_regions])
    check("prompt_hint: names the drawing region count", "1 non-text region" in hint, True)

    summary = rm_strokes.summarize([r.__dict__ for r in drawing_regions])
    check("summarize: counts the likely-drawing region", summary["likely_drawing_regions"], 1)
    check("summarize: empty input -> zero counts",
          rm_strokes.summarize(None), {"regions": 0, "likely_drawing_regions": 0, "tools": []})

    # --- STROKE_CONTEXT: daemon threads page_regions into the OCR prompt hint
    # and the frontmatter summary (via the fake_render/fake_ocr stubs) ---
    ocr_daemon.STROKE_CONTEXT = True
    (tmp / "vault/remarkable/Work/SketchNote.rm").write_bytes(b"rm-bytes-sketch")
    RENDER_REGIONS["SketchNote.rm"] = [
        [r.__dict__ for r in drawing_regions],
    ]
    check("STROKE_CONTEXT: sketch note processes one file",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    sketch_md = (out_base / "Work/SketchNote-handwriting_converted.md").read_text()
    check("STROKE_CONTEXT: frontmatter records stroke_regions_flagged",
          "stroke_regions_flagged: 1" in sketch_md, True)
    ocr_daemon.STROKE_CONTEXT = False

    # --- blank-page detection (rm_ocr's real ocr_pdf, not the fake_ocr stub) ---
    # PIL/Pillow isn't installed in this zero-dependency harness (real
    # pdf2image — which pulls it in — is stubbed out above), so fake just
    # enough of PIL.ImageStat's surface for _is_blank_page's real code path
    # (page.convert("L") -> ImageStat.Stat(...).mean/.stddev) to run unmodified.
    import json as _json
    import urllib.request as _urllib_request

    class _FakeStat:
        def __init__(self, img):
            self.mean = [img._mean]
            self.stddev = [img._stddev]

    _fake_imagestat_mod = types.ModuleType("PIL.ImageStat")
    _fake_imagestat_mod.Stat = _FakeStat
    _fake_pil_mod = types.ModuleType("PIL")
    _fake_pil_mod.ImageStat = _fake_imagestat_mod
    sys.modules["PIL"] = _fake_pil_mod
    sys.modules["PIL.ImageStat"] = _fake_imagestat_mod

    class _FakePage:
        """Just enough of PIL.Image's surface for _is_blank_page + ocr_pdf's per-page loop."""
        def __init__(self, mean, stddev, size=(200, 260)):
            self._mean, self._stddev = mean, stddev
            self.size = size

        def convert(self, mode):
            return self

        def resize(self, size):
            self.size = size
            return self

        def save(self, buf, format=None):
            buf.write(b"fake-png-bytes")

    blank_page_img = _FakePage(255.0, 0.0)
    content_page_img = _FakePage(250.0, 25.0)

    check("_is_blank_page: blank stats -> True", rm_ocr._is_blank_page(blank_page_img), True)
    check("_is_blank_page: content stats -> False", rm_ocr._is_blank_page(content_page_img), False)

    ocr_calls = []

    def fake_urlopen(req, timeout=None):
        ocr_calls.append(_json.loads(req.data)["prompt"])

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                yield _json.dumps({"response": "real page\ntext wrapped", "done": True}).encode()

        return _FakeResp()

    # ocr_pdf now renders one page at a time: pdfinfo gives the count, then each
    # page is fetched with first_page=last_page=n. Mirror that in the stubs.
    _doc_pages = [blank_page_img, content_page_img]
    saved_convert = rm_ocr.convert_from_path
    saved_pdfinfo = rm_ocr.pdfinfo_from_path
    saved_urlopen = _urllib_request.urlopen
    rm_ocr.convert_from_path = lambda *a, first_page=1, **k: [_doc_pages[first_page - 1]]
    rm_ocr.pdfinfo_from_path = lambda *a, **k: {"Pages": len(_doc_pages)}
    _urllib_request.urlopen = fake_urlopen
    try:
        blank_pages = _real_ocr_pdf("fake.pdf", "test-model", 150, 1568)
    finally:
        rm_ocr.convert_from_path = saved_convert
        rm_ocr.pdfinfo_from_path = saved_pdfinfo
        _urllib_request.urlopen = saved_urlopen

    check("skip_blank: OCR call skipped for the blank page (only 1 call made)",
          len(ocr_calls), 1)
    check("skip_blank: blank page gets the placeholder text",
          blank_pages[0], (1, rm_ocr.BLANK_PAGE_TEXT))
    check("skip_blank + reflow: real page is reflowed by ocr_pdf's default (reflow=True)",
          blank_pages[1], (2, "real page text wrapped"))

    # --- reflow_paragraphs (pure text transform, no model/PIL involved) ---
    check("reflow_paragraphs: joins word-wrapped lines within a paragraph",
          rm_ocr.reflow_paragraphs("It's really difficult to\nput into words\nwhen you find someone."),
          "It's really difficult to put into words when you find someone.")

    check("reflow_paragraphs: blank line still separates real paragraphs",
          rm_ocr.reflow_paragraphs("First para\nline two.\n\nSecond para\nline two."),
          "First para line two.\n\nSecond para line two.")

    check("reflow_paragraphs: headings/bullets/numbered lists/blockquotes untouched",
          rm_ocr.reflow_paragraphs(
              "## Page 1\n- a bullet\n1. a numbered item\n> a quote\nplain wrapped\ntext line"),
          "## Page 1\n- a bullet\n1. a numbered item\n> a quote\nplain wrapped text line")

    check("reflow_paragraphs: fenced code (incl. a blank line inside it) left exactly as-is",
          rm_ocr.reflow_paragraphs("prose line one\nprose line two\n\n```\nfenced line one\n\nfenced line two\n```"),
          "prose line one prose line two\n\n```\nfenced line one\n\nfenced line two\n```")

    check("reflow_paragraphs: empty input -> empty output", rm_ocr.reflow_paragraphs(""), "")

    # --- rm_render unit checks (visibleName precedence — exercised w/o the stub) ---
    import zipfile as _zip
    fixt_dir = tmp / "rmrender-fixtures"
    fixt_dir.mkdir()

    zp = fixt_dir / "real.zip"
    with _zip.ZipFile(zp, "w") as zf:
        zf.writestr("uuid.metadata", '{"visibleName": "From Metadata"}')
    check("rm_render._title_for reads visibleName from .metadata",
          rm_render._title_for(zp), "From Metadata")

    zp_nomet = fixt_dir / "nometadata.zip"
    with _zip.ZipFile(zp_nomet, "w") as zf:
        zf.writestr("uuid.content", "{}")
    check("rm_render._title_for falls back to stem when no .metadata",
          rm_render._title_for(zp_nomet), "nometadata")

    zp_empty = fixt_dir / "emptyname.zip"
    with _zip.ZipFile(zp_empty, "w") as zf:
        zf.writestr("uuid.metadata", '{"visibleName": "   "}')
    check("rm_render._title_for ignores blank visibleName, uses stem",
          rm_render._title_for(zp_empty), "emptyname")

    # forbidden-path guard
    saved = ocr_daemon.OUT
    ocr_daemon.OUT = pathlib.Path("/mnt/docker/scrybble/storage/efs/x")
    try:
        ocr_daemon.assert_safe_paths()
        check("forbidden path refused", False, True)
    except SystemExit:
        check("forbidden path refused", True, True)
    finally:
        ocr_daemon.OUT = saved

    print(f"\n--- sample transcript ---\n{md}")
    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
