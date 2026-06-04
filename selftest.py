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
    (tmp / "vault/remarkable/Work/Carol.pdf").write_text("pdf-bytes-v1")
    (tmp / "vault/remarkable/Work/Bad.pdf").write_text("broken")

    os.environ.update(
        VAULT_DIR=str(tmp / "vault"),
        SOURCE_SUBDIR="remarkable",
        OUT_DIR=str(tmp / "out"),          # output base OUTSIDE the (read-only) vault
        STATE_DIR=str(tmp / "state"),
        MODEL="qwen3.5:9b",
    )
    out_base = tmp / "out"

    # stub pdf2image so importing rm_ocr needs no poppler
    m = types.ModuleType("pdf2image")
    m.convert_from_path = lambda *a, **k: []
    sys.modules["pdf2image"] = m

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import rm_ocr

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

    def fake_render(src, *, cache_dir=None, workdir=None, use_chrome=False):
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
                                      source_sha256=sha)

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

    check("pass1 processes new files (Carol ok, Bad errors)",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 1)
    check("pass1 logs a queued gate (file changed -> OCR)",
          any("gate=queued" in m for m in gate_msgs), True)
    gate_msgs.clear()
    check("pass2 idempotent",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 0)
    check("pass2 skips via prefilter (no hash, no OCR)",
          any("gate=prefilter-skip" in m for m in gate_msgs), True)

    (tmp / "vault/remarkable/Work/Carol.pdf").write_text("pdf-bytes-v1")  # same bytes, new mtime
    check("touch-only change skipped",
          ocr_daemon.scan_once(ocr_daemon.load_manifest()), 0)

    (tmp / "vault/remarkable/Work/Carol.pdf").write_text("pdf-bytes-v2")  # real edit
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
    (tmp / "vault/remarkable/Work/Carol.pdf").write_text("pdf-bytes-v3-rapid-edit")
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

    carol_md = out_base / "Work/Carol-handwriting_converted.md"
    check("filename uses OUT_SUFFIX",
          ocr_daemon.safe_output_path(tmp / "vault/remarkable/Work/Carol.pdf").name,
          "Carol-handwriting_converted.md")
    check("transcript written to OUT_DIR base with suffix", carol_md.exists(), True)
    md = carol_md.read_text()
    check("transcript has frontmatter source", "source: remarkable/Work/Carol.pdf" in md, True)
    check("transcript records source last-modified", "source_modified:" in md, True)
    check("manifest records source last-modified",
          "source_modified" in ocr_daemon.load_manifest()["remarkable/Work/Carol.pdf"], True)
    check("transcript has backlink", "Source: [[remarkable/Work/Carol.pdf]]" in md, True)
    check("transcript has per-page bodies", "## Page 1" in md and "## Page 2" in md, True)
    check("transcript not written for failed pdf",
          (out_base / "Work/Bad-handwriting_converted.md").exists(), False)

    # alongside mode: transcript lands in the source PDF's own folder
    saved_al = ocr_daemon.OUT_ALONGSIDE
    ocr_daemon.OUT_ALONGSIDE = True
    op = ocr_daemon.safe_output_path(tmp / "vault/remarkable/Work/Carol.pdf")
    check("alongside mode writes next to source",
          op == tmp / "vault/remarkable/Work/Carol-handwriting_converted.md", True)
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
