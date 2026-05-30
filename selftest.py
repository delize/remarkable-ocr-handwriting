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
