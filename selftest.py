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

    # Image dispatch — .png/.jpeg/.webp reach the scanner and are titled from the
    # stem. Uses the stubbed renderer, so this needs no Pillow (the real wrap is
    # exercised in the image-render section further down).
    for img_name in ("Photo.png", "Snap.jpeg", "Shot.webp"):
        (tmp / "vault/remarkable/Work" / img_name).write_bytes(b"fake-image-" + img_name.encode())
    check("image inputs are discovered and processed",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 3)
    for stem, label in (("Photo", ".png"), ("Snap", ".jpeg"), ("Shot", ".webp")):
        check(f"{label} transcript named from stem",
              (out_base / f"Work/{stem}-handwriting_converted.md").exists(), True)
    check("image pass2 idempotent",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 0)
    check("inotify wake tuple covers image inputs",
          all(s in ocr_daemon._INPUT_SUFFIX_TUPLE for s in (".png", ".jpg", ".jpeg", ".webp")), True)

    # A photo sharing a stem with an already-transcribed .pdf must NOT overwrite
    # that transcript — the source-hash disambiguator kicks in.
    (tmp / "vault/remarkable/Work/Sample.png").write_bytes(b"photo-of-the-same-note")
    ocr_daemon.scan_once(ocr_daemon.load_manifest())
    check("image sharing a stem with a .pdf gets its own disambiguated transcript",
          len(list((out_base / "Work").glob("Sample*-handwriting_converted.md"))), 2)

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

    # Multi-page day: <date>-P001 / -P002 are separate transcripts that both embed
    # into the SAME <date>.md, under ONE heading, in page order.
    for page_no in ("P001", "P002"):
        (tmp / f"vault/remarkable/Work/2026-07-25-{page_no}.png").write_bytes(
            b"journal-page-" + page_no.encode())
    ocr_daemon.scan_once(ocr_daemon.load_manifest())
    note3 = daily_dir / "2026-07-25.md"
    body3 = note3.read_text()
    check("daily-note embed: -PXXX pages route to the date's note", note3.exists(), True)
    check("daily-note embed: no -PXXX-named note is created",
          (daily_dir / "2026-07-25-P001.md").exists(), False)
    check("daily-note embed: both pages embedded",
          all(f"2026-07-25-{p}-handwriting_converted" in body3 for p in ("P001", "P002")), True)
    check("daily-note embed: multi-page day gets exactly one heading",
          body3.count("## reMarkable journal"), 1)
    check("daily-note embed: pages embedded in order",
          body3.index("2026-07-25-P001") < body3.index("2026-07-25-P002"), True)
    check("daily-note embed: each page keeps its own transcript",
          all((ocr_daemon.OUT / f"Work/2026-07-25-{p}-handwriting_converted.md").exists()
              for p in ("P001", "P002")), True)
    check("daily-note embed: -PXXX re-OCR does not duplicate",
          body3.count("2026-07-25-P001-handwriting_converted"), 1)

    # A date-ish stem that isn't a page suffix must NOT be treated as a daily page.
    (tmp / "vault/remarkable/Work/2026-07-23-groceries.pdf").write_text("not-a-journal-page")
    ocr_daemon.scan_once(ocr_daemon.load_manifest())
    check("daily-note embed: non-page suffix is not a daily source",
          (daily_dir / "2026-07-23.md").exists(), False)

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

    # --- image render (the REAL rm_render._render_image, not the stub) ---
    # Must run BEFORE the blank-page section below, which puts a fake "PIL" into
    # sys.modules and would shadow the real Pillow from here on. Needs Pillow and
    # pypdf, which this zero-dependency harness does not install in CI — the
    # checks are skipped there and run in the Docker image or a dev checkout,
    # where both are present.
    try:
        from PIL import Image as _Img
        from pypdf import PdfReader as _PdfReader
    except ImportError:
        print("  [SKIP] image-render checks (needs Pillow + pypdf)")
    else:
        img_dir = tmp / "image-fixtures"
        img_dir.mkdir()

        def page_size(path):
            box = _PdfReader(str(path)).pages[0].mediabox
            return round(float(box.width), 1), round(float(box.height), 1)

        # Every image is normalized to IMAGE_PAGE_WIDTH_PT with the aspect kept,
        # which is what lets rm_split's point-based tuning apply to photos.
        src_img = img_dir / "wide.png"
        _Img.new("RGB", (1200, 900), "white").save(src_img)
        pdf, regions = rm_render._render_image(src_img, img_dir)
        w, h = page_size(pdf)
        check("image page normalized to IMAGE_PAGE_WIDTH_PT",
              w, round(rm_render.IMAGE_PAGE_WIDTH_PT, 1))
        check("image page keeps the source aspect ratio", round(h / w, 2), 0.75)
        check("image render reports no stroke regions", regions, None)

        # A tall source must stay tall in points, so the splitter can cut it.
        tall_src = img_dir / "tall.png"
        _Img.new("RGB", (1200, 8000), "white").save(tall_src)
        tw, th = page_size(rm_render._render_image(tall_src, img_dir)[0])
        check("tall image stays tall enough to trigger the split gate",
              th / tw > ocr_daemon.SPLIT_MAX_ASPECT, True)

        # Phone JPEGs store rotation as an EXIF tag, not as pixels. Without
        # exif_transpose the handwriting reaches the model sideways.
        rot_src = img_dir / "rotated.jpg"
        _exif = _Img.Exif()
        _exif[274] = 6  # Orientation: rotate 90° clockwise
        _Img.new("RGB", (400, 200), "white").save(rot_src, exif=_exif)
        rw, rh = page_size(rm_render._render_image(rot_src, img_dir)[0])
        check("EXIF orientation is applied (landscape source becomes portrait)",
              rh > rw, True)

        # Oversize sources are downscaled before embedding (aspect preserved), so
        # a 50 MP photo can't blow up the decode on a small container.
        big_src = img_dir / "big.jpg"
        _Img.new("RGB", (rm_render.IMAGE_MAX_WIDTH_PX * 2, 1000), "white").save(big_src)
        big_pdf = rm_render._render_image(big_src, img_dir)[0]
        check("oversize image downscaled to IMAGE_MAX_WIDTH_PX",
              _PdfReader(str(big_pdf)).pages[0].images[0].image.width,
              rm_render.IMAGE_MAX_WIDTH_PX)

        # Transparency flattens onto WHITE. A plain convert("RGB") composites
        # onto black and hands the model an unreadable page.
        alpha_src = img_dir / "alpha.png"
        _Img.new("RGBA", (300, 300), (0, 0, 0, 0)).save(alpha_src)
        alpha_pdf = rm_render._render_image(alpha_src, img_dir)[0]
        check("transparent pixels flattened onto white, not black",
              _PdfReader(str(alpha_pdf)).pages[0].images[0].image.convert("RGB").getpixel((150, 150)),
              (255, 255, 255))

        # WebP is decoded like any other raster input.
        webp_src = img_dir / "shot.webp"
        _Img.new("RGB", (800, 600), "white").save(webp_src)
        check("webp renders to a normalized page",
              page_size(rm_render._render_image(webp_src, img_dir)[0])[0],
              round(rm_render.IMAGE_PAGE_WIDTH_PT, 1))

        # Faint ink is stretched toward true black. This is the difference
        # between a transcript and an empty page: a real reMarkable page whose
        # darkest pixel was 192 made qwen3-vl:8b reason until it ran out of
        # context and returned nothing, where the normalized page transcribed
        # correctly inside the default context.
        def darkest_in_pdf(pdf_path):
            return _PdfReader(str(pdf_path)).pages[0].images[0].image.convert("L").getextrema()[0]

        faint_src = img_dir / "faint.png"
        faint = _Img.new("L", (600, 800), 255)
        for y in range(100, 700, 40):          # light-grey "ink" on white paper
            for x in range(60, 540):
                faint.putpixel((x, y), 205)
        faint.convert("RGB").save(faint_src)
        check("faint source really is faint", faint.getextrema()[0], 205)
        check("autocontrast pushes faint ink toward black",
              darkest_in_pdf(rm_render._render_image(faint_src, img_dir)[0]) < 60, True)

        saved_ac = rm_render.IMAGE_AUTOCONTRAST
        rm_render.IMAGE_AUTOCONTRAST = False
        off_dir = img_dir / "ac_off"
        off_dir.mkdir()
        check("IMAGE_AUTOCONTRAST=0 leaves the faint original alone",
              darkest_in_pdf(rm_render._render_image(faint_src, off_dir)[0]) > 150, True)
        rm_render.IMAGE_AUTOCONTRAST = saved_ac

        # It must not manufacture ink out of a blank page, or JPEG noise would
        # become "strokes" and the blank-page skip would stop firing.
        blank_src = img_dir / "blank.png"
        _Img.new("RGB", (600, 800), "white").save(blank_src)
        check("autocontrast leaves a blank page blank",
              darkest_in_pdf(rm_render._render_image(blank_src, img_dir)[0]) > 250, True)

        # A truncated/garbage image is a recognized ValueError, not a crash, so
        # the daemon records status=error and moves on.
        bad_src = img_dir / "corrupt.png"
        bad_src.write_bytes(b"\x89PNG\r\n\x1a\n-truncated-garbage")
        try:
            rm_render._render_image(bad_src, img_dir)
            check("corrupt image raises ValueError", False, True)
        except ValueError as e:
            check("corrupt image raises ValueError", "image render failed" in str(e), True)

        # AUTO_SPLIT's whole point for images: one tall page becomes several
        # readable ones. Needs PyMuPDF + numpy on top of the above.
        try:
            import numpy  # noqa: F401
            from rm_split import SplitConfig as _SplitConfig
            from rm_split import split_in_place as _split_in_place
            import fitz  # noqa: F401
        except ImportError:
            print("  [SKIP] image auto-split check (needs PyMuPDF + numpy)")
        else:
            banded = _Img.new("RGB", (1200, 6000), "white")
            for band in range(6):  # ink bands separated by whitespace gutters
                for y in range(band * 1000 + 100, band * 1000 + 400):
                    for x in range(100, 1100, 3):
                        banded.putpixel((x, y), (0, 0, 0))
            banded_src = img_dir / "banded.png"
            banded.save(banded_src)
            banded_pdf = rm_render._render_image(banded_src, img_dir)[0]
            check("tall image is one page before splitting",
                  len(_PdfReader(str(banded_pdf)).pages), 1)
            _split_in_place(banded_pdf, _SplitConfig())
            check("AUTO_SPLIT cuts a tall image into multiple readable pages",
                  len(_PdfReader(str(banded_pdf)).pages) > 1, True)

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

    # --- empty-answer detection (a reasoning model that never answers) ---
    # Real failure mode: qwen3-vl ignores think=False, reasons past the context
    # window, and is cut off with done_reason="length" — every token lands in
    # "thinking", "response" is empty, and the old code wrote a plausible-looking
    # empty page under status=ok. A page with ink that yields nothing must be
    # visible, and a document where EVERY page yields nothing must be an error.
    def make_urlopen(stream_objs):
        def _fake(req, timeout=None):
            class _FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def __iter__(self):
                    for o in stream_objs:
                        yield _json.dumps(o).encode()

            return _FakeResp()
        return _fake

    THINKING_ONLY = [{"thinking": "let me work through this page..." * 20},
                     {"done": True, "done_reason": "length"}]
    _doc_pages = [content_page_img]
    rm_ocr.convert_from_path = lambda *a, first_page=1, **k: [_doc_pages[first_page - 1]]
    rm_ocr.pdfinfo_from_path = lambda *a, **k: {"Pages": len(_doc_pages)}
    _urllib_request.urlopen = make_urlopen(THINKING_ONLY)
    try:
        _real_ocr_pdf("fake.pdf", "test-model", 150, 1568)
        check("all-pages-empty raises instead of writing an empty transcript", False, True)
    except RuntimeError as e:
        check("all-pages-empty raises instead of writing an empty transcript",
              "no text for any" in str(e), True)
    finally:
        _urllib_request.urlopen = saved_urlopen

    # A partial failure still returns, with the bad page marked in place so the
    # transcript says what happened rather than looking like a blank page.
    _doc_pages = [content_page_img, content_page_img]
    rm_ocr.convert_from_path = lambda *a, first_page=1, **k: [_doc_pages[first_page - 1]]
    rm_ocr.pdfinfo_from_path = lambda *a, **k: {"Pages": len(_doc_pages)}
    _calls = {"n": 0}

    def _mixed(req, timeout=None):
        _calls["n"] += 1
        objs = THINKING_ONLY if _calls["n"] == 1 else [
            {"response": "second page is fine"}, {"done": True, "done_reason": "stop"}]
        return make_urlopen(objs)(req, timeout)

    _urllib_request.urlopen = _mixed
    try:
        mixed = _real_ocr_pdf("fake.pdf", "test-model", 150, 1568)
    finally:
        rm_ocr.convert_from_path = saved_convert
        rm_ocr.pdfinfo_from_path = saved_pdfinfo
        _urllib_request.urlopen = saved_urlopen
    check("partial failure still returns every page", len(mixed), 2)
    check("failed page is marked, not left blank",
          mixed[0][1].startswith(rm_ocr.NO_OUTPUT_TEXT), True)
    check("failed page records why it was empty",
          "done_reason='length'" in mixed[0][1] and "reasoning discarded" in mixed[0][1], True)
    check("failed page is distinguishable from a genuinely blank page",
          rm_ocr.NO_OUTPUT_TEXT != rm_ocr.BLANK_PAGE_TEXT, True)
    check("good page in a partially-failed document is untouched",
          mixed[1][1], "second page is fine")

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
