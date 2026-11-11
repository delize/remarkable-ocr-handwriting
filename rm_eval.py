#!/usr/bin/env python3
"""
rm_eval.py — measure transcription quality against a ground-truth page set.

Every change to this pipeline (model, vocabulary, dual read, resolution) is a
guess until it is scored on pages whose correct text is known. This runs a
configuration over such a set and reports the numbers that decide it:

  primary_wer / cer   one model, as the daemon writes without verification
  final_wer / cer     after dual read and resolution, ignoring the flags
  flag_rate           share of words inside ==A|B== flags (what review costs)
  error_recall        share of word errors that sit inside a flag
  review_wer          errors left once every flag is fixed by hand

Ground truth comes from two places. The daemon files every page you correct
in Obsidian (STATE/goldset, origin "user-edit"), and pages can be imported
from a PDF plus a transcription you trust (origin "curated").

Examples:
  python3 rm_eval.py import-pdf note.pdf truth.json --goldset /state/goldset
  python3 rm_eval.py run --goldset /state/goldset --model gemma4:26b --out base.json
  python3 rm_eval.py run --goldset /state/goldset --model gemma4:26b \\
      --verify-model qwen3.6:35b-a3b --resolve --vocab-file /state/vocab.txt --out dual.json
  python3 rm_eval.py compare base.json dual.json
  python3 rm_eval.py pairs base.json ornith.json qwen.json --goldset /state/goldset
  python3 rm_eval.py export --goldset /state/goldset --out /state/train --holdout 0.25
"""
import argparse
import base64
import datetime
import difflib
import hashlib
import json
import os
import pathlib
import re
import sys
import time

import rm_verify


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def norm(text):
    """Words for scoring: lowercase, flags and [illegible] dropped, punctuation ignored.

    A slash separates words, so "Claude/OpenAI" and "Claude / OpenAI" score
    the same: spacing around a slash is not a misread.
    """
    text = rm_verify.strip_marks(text).lower().replace("’", "'")
    text = re.sub(r"\[illegible\]", " ", text)
    return re.sub(r"[^a-z0-9' ]+", " ", text).split()


def edits(a, b):
    """Levenshtein distance between two sequences."""
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def score(hyp, truth):
    """``{"words", "word_errors", "chars", "char_errors"}`` for one page."""
    r, h = norm(truth), norm(hyp)
    rc, hc = " ".join(r), " ".join(h)
    return {"words": len(r), "word_errors": edits(h, r), "chars": len(rc), "char_errors": edits(hc, rc)}


def flag_score(final, truth):
    """How well the flags on one page cover its errors.

    Word errors are located with a diff against the truth. An error counts as
    caught when a flagged word sits in it (or, for a dropped word, right next
    to it).
    """
    r = norm(truth)
    h, hf = [], []
    for word, flagged in zip(*rm_verify.flagged_words(final)):
        for tok in norm(word):
            h.append(tok)
            hf.append(flagged)
    errors = caught = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, r, h, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        n = max(i2 - i1, j2 - j1)
        near = hf[j1:j2] if j2 > j1 else hf[max(0, j1 - 1):j1 + 1]
        errors += n
        caught += n if any(near) else 0
    return {"flagged_words": sum(hf), "hyp_words": len(h), "diff_errors": errors, "caught": caught}


def summarize(cases):
    """Pooled metrics over per-case results (pooled, so long pages weigh more)."""
    def total(k):
        return sum(c.get(k, 0) for c in cases)

    def rate(num, den):
        return round(num / den, 4) if den else None

    out = {"cases": len(cases), "words": total("words"),
           "primary_wer": rate(total("primary_word_errors"), total("words")),
           "primary_cer": rate(total("primary_char_errors"), total("chars")),
           "seconds_per_page": rate(total("seconds"), len(cases))}
    if any("final_word_errors" in c for c in cases):
        out.update(
            final_wer=rate(total("final_word_errors"), total("words")),
            final_cer=rate(total("final_char_errors"), total("chars")),
            flag_rate=rate(total("flagged_words"), total("hyp_words")),
            error_recall=rate(total("caught"), total("diff_errors")),
            review_wer=rate(total("diff_errors") - total("caught"), total("words")),
        )
    fo = [c["flags_only"] for c in cases if "flags_only" in c]
    if fo:
        out.update(
            flags_only_flag_rate=rate(sum(x["flagged_words"] for x in fo), sum(x["hyp_words"] for x in fo)),
            flags_only_review_wer=rate(sum(x["diff_errors"] - x["caught"] for x in fo), total("words")),
        )
    return out


# ---------------------------------------------------------------------------
# Ground-truth set
# ---------------------------------------------------------------------------
def load_goldset(gold_dir, include_flagged=False, limit=0):
    """Cases with an image and a truth. Pages that still carry flags are skipped by default."""
    cases = []
    for meta in sorted(pathlib.Path(gold_dir).glob("*.json")):
        info = rm_verify.load_json(meta, None)
        png = meta.with_suffix(".png")
        if not info or not png.exists() or not info.get("truth", "").strip():
            continue
        if info.get("unresolved_flags") and not include_flagged:
            continue
        cases.append({"id": meta.stem, "png": png, **info})
    return cases[:limit] if limit else cases


def import_pdf(pdf, truth_pages, gold_dir, dpi=220, max_px=1568, source=None):
    """Store each page of ``pdf`` with its trusted transcription as a curated case."""
    import rm_ocr
    from pdf2image import pdfinfo_from_path

    n_pages = pdfinfo_from_path(str(pdf))["Pages"]
    if n_pages != len(truth_pages):
        raise SystemExit(f"{pdf} has {n_pages} pages but the truth has {len(truth_pages)}")
    keys = []
    for n, truth in enumerate(truth_pages, 1):
        png = base64.b64decode(rm_ocr.render_page_b64(pdf, n, dpi, max_px))
        keys.append(rm_verify.write_gold_case(gold_dir, source or str(pdf), n, truth, png,
                                              origin="curated"))
    return keys


def is_holdout(case_id, fraction):
    """Stable train/holdout split by id, so a page never moves between the two."""
    return int(hashlib.sha1(case_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < fraction


def export(cases, out_dir, holdout=0.25, prompt=None):
    """Write the ground-truth set as local fine-tuning data.

    ``out_dir/images/<id>.png`` plus ``train.jsonl`` and ``holdout.jsonl``.
    Each line carries the plain pair (``image``, ``text``) and the same pair
    as a chat ``messages`` list, the shape vision fine-tuning tools read. The
    holdout pages must stay out of training: score a tuned model on them with
    ``run --ids holdout.ids`` or its gain is measured on pages it memorized.
    """
    import shutil

    import rm_ocr
    prompt = prompt or rm_ocr.PROMPT
    out = pathlib.Path(out_dir)
    (out / "images").mkdir(parents=True, exist_ok=True)
    counts = {"train": 0, "holdout": 0}
    ids = {k: [] for k in counts}
    with open(out / "train.jsonl", "w") as train, open(out / "holdout.jsonl", "w") as held:
        files = {"train": train, "holdout": held}
        for c in cases:
            split = "holdout" if is_holdout(c["id"], holdout) else "train"
            image = f"images/{c['id']}.png"
            shutil.copyfile(c["png"], out / image)
            files[split].write(json.dumps({
                "id": c["id"], "image": image, "text": c["truth"],
                "source": c.get("source"), "page": c.get("page"), "origin": c.get("origin"),
                "messages": [
                    {"role": "user", "content": [{"type": "image", "image": image},
                                                 {"type": "text", "text": prompt}]},
                    {"role": "assistant", "content": [{"type": "text", "text": c["truth"]}]},
                ],
            }, ensure_ascii=False) + "\n")
            counts[split] += 1
            ids[split].append(c["id"])
    for k, v in ids.items():
        (out / f"{k}.ids").write_text("\n".join(v) + ("\n" if v else ""))
    return counts


# ---------------------------------------------------------------------------
# Running a configuration
# ---------------------------------------------------------------------------
def _transcribe_all(cases, model, prompt_extra, cfg, log, confidences=None):
    """``{case_id: (text, seconds)}``; fills ``confidences[case_id]`` when given a dict."""
    import rm_ocr
    out = {}
    for c in cases:
        t0 = time.time()
        img = base64.b64encode(c["png"].read_bytes()).decode()
        conf = [] if confidences is not None else None
        out[c["id"]] = (rm_ocr.ocr_image_b64(img, model, timeout=cfg["timeout"], no_think=True,
                                             num_ctx=cfg["num_ctx"], prompt_extra=prompt_extra,
                                             confidence_out=conf),
                        time.time() - t0)
        if confidences is not None:
            confidences[c["id"]] = conf
        log(f"  {model} {c['id']} {out[c['id']][1]:.0f}s")
    return out


def _unload(model, log):
    import rm_ocr
    try:
        rm_ocr.unload_model(model)
    except Exception as e:  # an unload failure only costs memory, never the run
        log(f"  unload {model} failed: {e}")


def run(cases, *, model, verify_model="", resolve=False, resolve_model="", terms=(),
        timeout=1800, num_ctx=16384, max_span_words=6, confidence_threshold=None, log=print):
    """Score one configuration. Models run in stages so a CPU host holds one at a time.

    With ``confidence_threshold`` the primary model's word confidences are
    recorded (``primary_confs`` per case, reusable by ``sweep``) and its
    low-confidence words are flagged on top of any dual-read flags, exactly as
    the daemon does.
    """
    import rm_ocr
    cfg = {"timeout": timeout, "num_ctx": num_ctx}
    hint = rm_verify.vocab_hint(list(terms))
    confs = {} if confidence_threshold is not None else None
    primary = _transcribe_all(cases, model, hint, cfg, log, confidences=confs)
    secondary = {}
    if verify_model:
        _unload(model, log)
        secondary = _transcribe_all(cases, verify_model, hint, cfg, log)
    resolver_model = resolve_model or verify_model or model
    results = []
    for c in cases:
        text, secs = primary[c["id"]]
        row = {"id": c["id"], "source": c.get("source"), "page": c.get("page"),
               "origin": c.get("origin"), "seconds": secs}
        s = score(text, c["truth"])
        row.update(words=s["words"], chars=s["chars"], primary_word_errors=s["word_errors"],
                   primary_char_errors=s["char_errors"])
        if verify_model:
            b_text, b_secs = secondary[c["id"]]
            row["seconds"] += b_secs
            resolver = None
            if resolve:
                img = base64.b64encode(c["png"].read_bytes()).decode()

                def resolver(spans, img=img):
                    return rm_ocr.generate_json(
                        resolver_model, rm_verify.resolve_prompt(spans, hint), img,
                        rm_verify.RESOLVE_SCHEMA, timeout=timeout, num_ctx=num_ctx)
            t0 = time.time()
            final, vstats = rm_verify.verify_page(text, b_text, resolver=resolver,
                                                  max_span_words=max_span_words)
            row["seconds"] += time.time() - t0
            f = score(final, c["truth"])
            row.update(final_word_errors=f["word_errors"], final_char_errors=f["char_errors"],
                       **flag_score(final, c["truth"]), verify=vstats, final=final)
            if resolve:
                # The same two readings with flags only, so one run shows what
                # resolution adds over flagging.
                flagged_only, _ = rm_verify.verify_page(text, b_text, max_span_words=max_span_words)
                row["flags_only"] = flag_score(flagged_only, c["truth"])
        if confs is not None:
            base = row.get("final", text)
            marked, _ = rm_verify.mark_low_confidence(base, confs.get(c["id"]), confidence_threshold)
            f = score(marked, c["truth"])
            row.update(final_word_errors=f["word_errors"], final_char_errors=f["char_errors"],
                       **flag_score(marked, c["truth"]), final=marked,
                       primary_confs=confs.get(c["id"]))
        row["primary"] = text
        if verify_model:
            row["secondary"] = b_text
        results.append(row)
    config = {"model": model, "verify_model": verify_model, "resolve": bool(resolve),
              "resolve_model": resolver_model if resolve else "", "terms": len(terms),
              "num_ctx": num_ctx, "max_span_words": max_span_words,
              "confidence_threshold": confidence_threshold}
    return {"config": config, "summary": summarize(results), "cases": results,
            "at": datetime.datetime.now().isoformat(timespec="seconds")}


def gate_terms(cases, model, base_terms, candidate_terms, *, tolerance=0.0, timeout=1800,
               num_ctx=16384, log=print):
    """Accept new learned terms only if they do not make the primary model worse.

    Runs the primary model over ``cases`` with the current vocabulary and with
    the candidate one. Returns ``(accepted, base_summary, candidate_summary)``.
    """
    base = run(cases, model=model, terms=base_terms, timeout=timeout, num_ctx=num_ctx, log=log)
    cand = run(cases, model=model, terms=list(base_terms) + list(candidate_terms),
               timeout=timeout, num_ctx=num_ctx, log=log)
    b, c = base["summary"]["primary_cer"], cand["summary"]["primary_cer"]
    accepted = b is not None and c is not None and c <= b + tolerance
    return accepted, base["summary"], cand["summary"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
_COLUMNS = ["primary_wer", "primary_cer", "final_wer", "final_cer", "flag_rate",
            "error_recall", "review_wer", "seconds_per_page"]


def _fmt(k, v):
    if v is None:
        return "-"
    return f"{v:.0f}" if k == "seconds_per_page" else f"{v:.1%}"


def reads_from_runs(runs):
    """``{model: {case_id: text}}`` from saved runs, including dual-read second readings."""
    reads = {}
    for r in runs:
        cfg = r["config"]
        for c in r["cases"]:
            if "primary" in c:
                reads.setdefault(cfg["model"], {})[c["id"]] = c["primary"]
            if cfg.get("verify_model") and "secondary" in c:
                reads.setdefault(cfg["verify_model"], {})[c["id"]] = c["secondary"]
    return reads


def pair_scores(reads, truth, max_span_words=6):
    """Score every ordered (primary, verifier) pair as a flags-only dual read.

    Uses transcripts already saved by earlier runs, so no model is called.
    Only pages both models read and that have a truth are scored. The result
    says which second reader catches the most of a primary's errors, and
    whether two models simply agree on their mistakes (same family, same
    weights at another precision).
    """
    out = []
    models = sorted(reads)
    for a in models:
        for b in models:
            if a == b:
                continue
            ids = sorted(set(reads[a]) & set(reads[b]) & set(truth))
            rows = []
            for i in ids:
                final, _ = rm_verify.verify_page(reads[a][i], reads[b][i], max_span_words=max_span_words)
                s = score(reads[a][i], truth[i])
                rows.append({"words": s["words"], "chars": s["chars"], "seconds": 0,
                             "primary_word_errors": s["word_errors"],
                             "primary_char_errors": s["char_errors"],
                             "final_word_errors": s["word_errors"],
                             "final_char_errors": s["char_errors"],
                             **flag_score(final, truth[i])})
            if rows:
                out.append({"primary": a, "verifier": b, **summarize(rows)})
    return sorted(out, key=lambda r: (r["review_wer"] is None, r["review_wer"]))


def sweep(run, truth, thresholds, max_span_words=6):
    """Re-score saved readings at several confidence thresholds, without calling a model.

    Uses each case's ``primary_confs`` (and ``secondary`` when the run had a
    verify model), so a threshold can be re-tuned from corrections as the
    ground-truth set grows.
    """
    out = []
    for t in thresholds:
        rows = []
        for c in run["cases"]:
            if c["id"] not in truth or c.get("primary_confs") is None:
                continue
            base = c["primary"]
            if c.get("secondary") is not None:
                base, _ = rm_verify.verify_page(base, c["secondary"], max_span_words=max_span_words)
            if t is not None:
                base, _ = rm_verify.mark_low_confidence(base, c["primary_confs"], t)
            s = score(c["primary"], truth[c["id"]])
            rows.append({"words": s["words"], "chars": s["chars"], "seconds": 0,
                         "primary_word_errors": s["word_errors"],
                         "primary_char_errors": s["char_errors"],
                         "final_word_errors": s["word_errors"], "final_char_errors": s["char_errors"],
                         **flag_score(base, truth[c["id"]])})
        if rows:
            out.append({"threshold": t, **summarize(rows)})
    return out


def compare(paths):
    runs = [json.loads(pathlib.Path(p).read_text()) for p in paths]
    width = max(len(pathlib.Path(p).name) for p in paths)
    print(" " * width + "".join(f"{k:>18}" for k in _COLUMNS))
    for p, r in zip(paths, runs):
        s = r["summary"]
        print(f"{pathlib.Path(p).name:<{width}}" + "".join(f"{_fmt(k, s.get(k)):>18}" for k in _COLUMNS))
    return runs


def _terms_from(args):
    terms = []
    if args.vocab_file:
        terms += rm_verify.parse_terms(pathlib.Path(args.vocab_file).read_text())
    if args.learned:
        terms += [t for t in rm_verify.LearnedVocab(args.learned).active() if t not in terms]
    return terms


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Score a configuration on the ground-truth set")
    r.add_argument("--goldset", default=os.environ.get("GOLDSET_DIR", "/state/goldset"))
    r.add_argument("--model", default=os.environ.get("MODEL", "gemma4:26b"))
    r.add_argument("--verify-model", default="")
    r.add_argument("--resolve", action="store_true", help="Settle disagreements against the image")
    r.add_argument("--resolve-model", default="", help="Default: the verify model")
    r.add_argument("--vocab-file", default="")
    r.add_argument("--learned", default="", help="learned_vocab.json; its active terms join the hint")
    r.add_argument("--num-ctx", type=int, default=16384)
    r.add_argument("--timeout", type=int, default=1800)
    r.add_argument("--max-span-words", type=int, default=6)
    r.add_argument("--confidence-threshold", type=float, default=None,
                   help="Flag the primary model's words below this log-probability (e.g. -0.2)")
    r.add_argument("--limit", type=int, default=0, help="Score only the first N pages")
    r.add_argument("--include-flagged", action="store_true",
                   help="Also score edited pages that still carry unresolved flags")
    r.add_argument("--ids", default="", help="Score only the case ids listed in this file "
                                              "(e.g. holdout.ids from export)")
    r.add_argument("--out", required=True)
    r.add_argument("--history", default="", help="Append the summary to this JSONL file")

    c = sub.add_parser("compare", help="Side-by-side summaries of saved runs")
    c.add_argument("runs", nargs="+")
    c.add_argument("--gate", action="store_true",
                   help="Exit 1 unless the last run's final (or primary) CER beats the first's")

    sw = sub.add_parser("sweep", help="Re-score a saved run at several confidence thresholds")
    sw.add_argument("run")
    sw.add_argument("--goldset", default=os.environ.get("GOLDSET_DIR", "/state/goldset"))
    sw.add_argument("--thresholds", default="none,-0.5,-0.2,-0.1,-0.05",
                    help="Comma-separated; 'none' scores dual-read flags alone")

    pr = sub.add_parser("pairs", help="Score every model pair as dual read from saved runs")
    pr.add_argument("runs", nargs="+")
    pr.add_argument("--goldset", default=os.environ.get("GOLDSET_DIR", "/state/goldset"))
    pr.add_argument("--max-span-words", type=int, default=6)
    pr.add_argument("--out", default="", help="Also save the table as JSON")

    i = sub.add_parser("import-pdf", help="Add a PDF's pages with a trusted transcription")
    i.add_argument("pdf")
    i.add_argument("truth", help="JSON: a list of page texts, or an object holding one")
    i.add_argument("--key", default="", help="Which entry of a JSON object to use")
    i.add_argument("--goldset", default=os.environ.get("GOLDSET_DIR", "/state/goldset"))
    i.add_argument("--dpi", type=int, default=int(os.environ.get("DPI", "220")))
    i.add_argument("--max-px", type=int, default=int(os.environ.get("MAX_PX", "1568")))
    i.add_argument("--source", default="", help="Name recorded for the case (default: the path)")

    x = sub.add_parser("export", help="Write the ground-truth set as local fine-tuning data")
    x.add_argument("--goldset", default=os.environ.get("GOLDSET_DIR", "/state/goldset"))
    x.add_argument("--out", required=True)
    x.add_argument("--holdout", type=float, default=0.25,
                   help="Share of pages kept out of training for evaluation")
    x.add_argument("--include-flagged", action="store_true")

    ls = sub.add_parser("list", help="Show the ground-truth set")
    ls.add_argument("--goldset", default=os.environ.get("GOLDSET_DIR", "/state/goldset"))

    args = ap.parse_args()
    if args.cmd == "run":
        import rm_ocr
        rm_ocr.assert_local_host(rm_ocr.OLLAMA_URL,
                                 allow_remote=os.environ.get("ALLOW_REMOTE_MODEL_HOST") == "1")
        cases = load_goldset(args.goldset, include_flagged=args.include_flagged, limit=args.limit)
        if args.ids:
            wanted = set(pathlib.Path(args.ids).read_text().split())
            cases = [c for c in cases if c["id"] in wanted]
        if not cases:
            sys.exit(f"no ground-truth pages in {args.goldset}")
        result = run(cases, model=args.model, verify_model=args.verify_model, resolve=args.resolve,
                     resolve_model=args.resolve_model, terms=_terms_from(args), timeout=args.timeout,
                     num_ctx=args.num_ctx, max_span_words=args.max_span_words,
                     confidence_threshold=args.confidence_threshold)
        rm_verify.save_json(args.out, result)
        if args.history:
            with open(args.history, "a") as f:
                f.write(json.dumps({"at": result["at"], "config": result["config"],
                                    "summary": result["summary"]}) + "\n")
        compare([args.out])
    elif args.cmd == "compare":
        runs = compare(args.runs)
        if args.gate and len(runs) > 1:
            def cer(s):
                return s.get("final_cer") if s.get("final_cer") is not None else s.get("primary_cer")
            first, last = cer(runs[0]["summary"]), cer(runs[-1]["summary"])
            sys.exit(0 if first is not None and last is not None and last < first else 1)
    elif args.cmd == "sweep":
        run_data = json.loads(pathlib.Path(args.run).read_text())
        truth = {c["id"]: c["truth"] for c in load_goldset(args.goldset, include_flagged=True)}
        ts = [None if x.strip().lower() == "none" else float(x) for x in args.thresholds.split(",")]
        print(f"{'threshold':>10}{'pages':>7}{'flag_rate':>11}{'error_recall':>14}{'review_wer':>12}")
        for r in sweep(run_data, truth, ts):
            label = "none" if r["threshold"] is None else f"{r['threshold']:g}"
            print(f"{label:>10}{r['cases']:>7}{_fmt('flag_rate', r['flag_rate']):>11}"
                  f"{_fmt('error_recall', r['error_recall']):>14}{_fmt('review_wer', r['review_wer']):>12}")
    elif args.cmd == "pairs":
        runs = [json.loads(pathlib.Path(x).read_text()) for x in args.runs]
        truth = {c["id"]: c["truth"] for c in load_goldset(args.goldset, include_flagged=True)}
        table = pair_scores(reads_from_runs(runs), truth, max_span_words=args.max_span_words)
        print(f"{'primary + verifier':<44}{'pages':>6}{'primary_wer':>13}{'flag_rate':>11}"
              f"{'error_recall':>14}{'review_wer':>12}")
        for r in table:
            print(f"{r['primary'] + ' + ' + r['verifier']:<44}{r['cases']:>6}"
                  + "".join(f"{_fmt(k, r.get(k)):>{w}}" for k, w in
                            (("primary_wer", 13), ("flag_rate", 11), ("error_recall", 14),
                             ("review_wer", 12))))
        if args.out:
            rm_verify.save_json(args.out, table)
    elif args.cmd == "import-pdf":
        data = json.loads(pathlib.Path(args.truth).read_text())
        pages = data[args.key] if args.key else data
        if not isinstance(pages, list):
            sys.exit("truth must be a list of page texts (use --key for an object)")
        keys = import_pdf(args.pdf, pages, args.goldset, dpi=args.dpi, max_px=args.max_px,
                          source=args.source or None)
        print(f"imported {len(keys)} page(s) into {args.goldset}")
    elif args.cmd == "export":
        cases = load_goldset(args.goldset, include_flagged=args.include_flagged)
        counts = export(cases, args.out, holdout=args.holdout)
        print(f"exported {counts['train']} training and {counts['holdout']} holdout page(s) "
              f"to {args.out}")
    elif args.cmd == "list":
        for c in load_goldset(args.goldset, include_flagged=True):
            flags = f"  ({c['unresolved_flags']} open flags)" if c.get("unresolved_flags") else ""
            print(f"{c['id']}  {c.get('origin', '?'):9}  {c.get('source')} p{c.get('page')}{flags}")


if __name__ == "__main__":
    main()
