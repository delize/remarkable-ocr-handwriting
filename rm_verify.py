#!/usr/bin/env python3
"""
rm_verify.py — self-checking transcripts.

Pure logic for the verification pipeline, kept free of HTTP and PDF handling so
the offline self-test covers it directly. The daemon wires it to real models.

1. Dual read. The same page is transcribed by two models. Where their word
   sequences agree the text is kept. Where they differ, the span is uncertain.
   Measured on 26 hand-checked pages (gemma4:26b vs qwen3.6:35b-a3b), the two
   disagreed on about 8% of words, and 85% of all misread words fell inside
   those disagreements. Disagreement is the cheap, measured signal for "look
   here twice".
2. Resolution. Each uncertain span is put back to a model together with the
   page image as a constrained choice: reading A, reading B, the exact text if
   neither, or UNSURE. The question is about the handwriting, never about which
   reading sounds better, because models misread handwriting mostly by writing
   the plausible word instead of the written one. A text-only "fix it up" pass
   makes that worse.
3. Flags. Whatever resolution cannot settle is written as ``==A|B==``, which
   Obsidian renders as a highlight, so review means reading only the flags.
4. Learning. The words written to each page are kept in a sidecar. When the
   transcript is edited, the difference is harvested: term-like corrections
   feed a learned vocabulary, edited pages become ground truth for evaluation,
   and a later re-OCR keeps the edits of any page whose model output did not
   change.
"""
import datetime
import difflib
import hashlib
import json
import pathlib
import re
from dataclasses import dataclass

MARK = "=={a}|{b}=="
EMPTY = "?"                       # stands in for "this reading has nothing here"
MARK_RE = re.compile(r"==([^=|\n]*)\|([^=\n]*)==")
_PUNCT = ".,;:!?\"'()[]{}*_`~<>-\u2013\u2014#=+|\\/"
_PAGE_RE = re.compile(r"^## Page (\d+)\s*$", re.MULTILINE)


def _key(token):
    """Comparison form of a word: lowercase, straight quotes, edge punctuation off."""
    return token.lower().replace("’", "'").strip(_PUNCT)


def _squash(keys):
    return re.sub(r"[^a-z0-9]", "", "".join(keys))


@dataclass
class _Word:
    start: int
    end: int
    text: str
    key: str


def _words(text):
    """Word tokens with character offsets. Pure punctuation (-, ->, #) is skipped."""
    out = []
    for m in re.finditer(r"\S+", text):
        k = _key(m.group())
        if k:
            out.append(_Word(m.start(), m.end(), m.group(), k))
    return out


@dataclass
class Span:
    """One place where the two readings differ, located in the primary text."""
    start: int            # character range in the primary text; start == end for an insertion
    end: int
    a: str                # primary reading ("" when the primary has nothing here)
    b: str                # secondary reading ("" when the secondary has nothing here)
    before: str           # a few words of context either side, for the resolver
    after: str


def align(a_text, b_text, max_span_words=6):
    """Find where two readings of the same page disagree.

    Returns ``(spans, stats)``. Differences that vanish once punctuation and
    spacing are ignored ("Anthropic / OpenAI" vs "Anthropic/Open AI") are not
    spans. Neither are very long runs, which are layout differences such as a
    diagram read in a different order rather than misread words, and neither
    are spans that cross a paragraph break. Both are only counted, and the
    primary text stands.
    """
    wa, wb = _words(a_text), _words(b_text)
    stats = {"words": len(wa), "spans": 0, "formatting_only": 0, "too_long": 0,
             "cross_paragraph": 0}
    spans = []
    ops = difflib.SequenceMatcher(None, [w.key for w in wa], [w.key for w in wb],
                                  autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in ops:
        if op == "equal":
            continue
        a_words, b_words = wa[i1:i2], wb[j1:j2]
        if _squash(w.key for w in a_words) == _squash(w.key for w in b_words):
            stats["formatting_only"] += 1
            continue
        if len(a_words) > max_span_words or len(b_words) > max_span_words:
            stats["too_long"] += 1
            continue
        if a_words:
            start, end = a_words[0].start, a_words[-1].end
        else:
            start = end = wa[i1].start if i1 < len(wa) else len(a_text.rstrip())
        if "\n" in a_text[start:end]:
            stats["cross_paragraph"] += 1   # a highlight cannot span paragraphs
            continue
        spans.append(Span(
            start=start, end=end, a=a_text[start:end], b=" ".join(w.text for w in b_words),
            before=" ".join(w.text for w in wa[max(0, i1 - 5):i1]),
            after=" ".join(w.text for w in wa[i2:i2 + 5]),
        ))
    stats["spans"] = len(spans)
    return spans, stats


RESOLVE_SCHEMA = {
    "type": "object",
    "properties": {"answers": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "choice": {"type": "string", "enum": ["A", "B", "OTHER", "UNSURE"]},
            "text": {"type": "string"},
        },
        "required": ["id", "choice"],
    }}},
    "required": ["answers"],
}


def resolve_prompt(spans, hint=""):
    """The question put to the resolver, one numbered item per span."""
    lines = [
        "Two independent readings of this handwritten page disagree in the places listed below.",
        "For each item, look closely at that spot in the handwriting and decide what is actually written.",
        'Answer "A" or "B" when that reading matches the handwriting. Answer "OTHER" with the exact '
        'written text in "text" when neither matches. Answer "UNSURE" when the handwriting cannot be '
        "read with confidence. Judge only by what is written, never by which reading sounds more natural.",
        "",
    ]
    for i, s in enumerate(spans, 1):
        lines.append(f'{i}. ...{s.before} [A: "{s.a or "(nothing)"}" | B: "{s.b or "(nothing)"}"] '
                     f"{s.after}...")
    if hint:
        lines += ["", hint]
    lines += ["", 'Reply as JSON: {"answers": [{"id": 1, "choice": "A"}, ...]}, one answer per item.']
    return "\n".join(lines)


def decisions_from_answers(spans, answers):
    """Turn a resolver reply into one decision per span: replacement text, or None to flag it.

    Anything missing, malformed or UNSURE stays flagged. An OTHER answer is only
    accepted when it is short, so a resolver that starts transcribing the page
    cannot overwrite a sentence through one item.
    """
    decisions = [None] * len(spans)
    kinds = [None] * len(spans)
    items = (answers or {}).get("answers") if isinstance(answers, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("id")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= i < len(spans) or decisions[i] is not None:
            continue
        s, choice = spans[i], str(item.get("choice", "")).upper()
        if choice == "A":
            decisions[i], kinds[i] = s.a, "a"
        elif choice == "B":
            decisions[i], kinds[i] = s.b, "b"
        elif choice == "OTHER":
            text = str(item.get("text") or "").strip()
            limit = max(len(s.a.split()), len(s.b.split())) + 2
            if text and len(text.split()) <= limit:
                decisions[i], kinds[i] = text, "other"
    return decisions, kinds


def render(a_text, spans, decisions):
    """Primary text with every span replaced by its decision, or flagged when undecided."""
    out, pos = [], 0
    for s, d in zip(spans, decisions):
        out.append(a_text[pos:s.start])
        if d is None:
            mark = MARK.format(a=s.a or EMPTY, b=s.b or EMPTY)
            out.append(mark + " " if s.start == s.end and s.start < len(a_text) else mark)
        elif s.start == s.end and d and s.start < len(a_text):
            out.append(d + " ")             # an inserted word needs its own space
        else:
            out.append(d)
        pos = s.end
    out.append(a_text[pos:])
    return re.sub(r"(?<=\S)  +(?=\S)", " ", "".join(out))


def verify_page(a_text, b_text, resolver=None, max_span_words=6, max_questions=30):
    """Dual-read one page: returns ``(text, stats)``.

    ``resolver`` is ``callable(spans) -> answers`` (the parsed JSON reply), or
    None to flag every disagreement. Only the first ``max_questions`` spans are
    asked about. The rest are flagged.
    """
    spans, stats = align(a_text, b_text, max_span_words=max_span_words)
    decisions = [None] * len(spans)
    kinds = [None] * len(spans)
    if resolver is not None and spans:
        asked = spans[:max_questions]
        d, k = decisions_from_answers(asked, resolver(asked))
        decisions[:len(asked)], kinds[:len(asked)] = d, k
    for kind in ("a", "b", "other"):
        stats[f"resolved_{kind}"] = kinds.count(kind)
    stats["flagged"] = decisions.count(None)
    return render(a_text, spans, decisions), stats


def strip_marks(text, side="a"):
    """Remove flags, keeping one reading; ``side`` is "a" or "b"."""
    def pick(m):
        v = m.group(1) if side == "a" else m.group(2)
        return "" if v == EMPTY else v
    return MARK_RE.sub(pick, text)


def flagged_words(text):
    """``(words, flags)``: the text's words (primary reading) and whether each sat inside a flag."""
    words, flags, pos = [], [], 0
    for m in MARK_RE.finditer(text):
        plain = text[pos:m.start()].split()
        words += plain
        flags += [False] * len(plain)
        inside = [] if m.group(1) == EMPTY else m.group(1).split()
        words += inside
        flags += [True] * len(inside)
        if not inside:              # the primary had nothing here: flag the neighbour
            if flags:
                flags[-1] = True
        pos = m.end()
    tail = text[pos:].split()
    return words + tail, flags + [False] * len(tail)


# ---------------------------------------------------------------------------
# Vocabulary hint
# ---------------------------------------------------------------------------
def parse_terms(text):
    """Terms from a vocabulary file: comma or newline separated, ``#`` starts a comment."""
    seen, out = set(), []
    for line in text.splitlines():
        for term in line.split("#", 1)[0].split(","):
            term = term.strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                out.append(term)
    return out


def vocab_hint(terms):
    """The prompt block that biases ambiguous words toward the writer's own terms.

    Measured on 16 jargon-heavy pages: WER 9.6% -> 8.6% (gemma4:26b) and
    8.5% -> 7.8% (qwen3.6:35b-a3b) at no speed cost. It pulls near misses
    toward listed terms (CIMD became SCIM when only SCIM was listed), so the
    list works best when it is the writer's full working vocabulary.
    """
    if not terms:
        return ""
    return (
        "The writer often uses these terms. When a handwritten word is ambiguous and closely "
        "matches one of them, use this spelling. Never add a term that is not actually written "
        "on the page.\n" + ", ".join(terms) + ".\n"
        "Write arrows as -> or <- in plain text. Do not use LaTeX or math notation."
    )


# ---------------------------------------------------------------------------
# Transcript sidecars, edit harvesting and carry-over
# ---------------------------------------------------------------------------
def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _norm_ws(text):
    return " ".join(text.split())


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def parse_pages(md_text):
    """``{page_number: text}`` from a transcript's ``## Page N`` sections."""
    parts = _PAGE_RE.split(md_text)
    return {int(parts[i]): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def sidecar_path(state_dir, out_rel):
    return pathlib.Path(state_dir) / "transcripts" / (
        hashlib.sha1(out_rel.encode()).hexdigest()[:16] + ".json")


def load_json(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def write_sidecar(state_dir, out_rel, source_rel, pages, file_bytes):
    """Record exactly what the daemon wrote, so a later edit can be told apart from it."""
    sha = sha256_bytes(file_bytes)
    save_json(sidecar_path(state_dir, out_rel), {
        "out_path": out_rel, "source": source_rel, "written_at": _now(),
        "written_sha": sha, "seen_sha": sha,
        "pages": {str(n): text for n, text in pages},
    })


def edited_pages(sidecar, current_pages):
    """Pages whose current text differs from what the daemon wrote: ``{n: (written, current)}``."""
    out = {}
    for key, written in (sidecar or {}).get("pages", {}).items():
        n = int(key)
        cur = current_pages.get(n)
        if cur is not None and _norm_ws(cur) != _norm_ws(written):
            out[n] = (written, cur)
    return out


def carry_over(new_pages, sidecar, current_pages):
    """Keep human edits across a re-OCR.

    For each page the user edited, keep the edit when the model's new output
    for that page is identical to what it wrote last time (the page itself did
    not change). Returns ``(pages, kept, superseded)``. A superseded edit is
    one whose page did change. It has already been harvested, so the
    correction still counts toward learning and evaluation.
    """
    edits = edited_pages(sidecar, current_pages)
    out, kept, superseded = [], 0, 0
    for n, text in new_pages:
        if n in edits:
            written, current = edits[n]
            if _norm_ws(text) == _norm_ws(written):
                out.append((n, current))
                kept += 1
                continue
            superseded += 1
        out.append((n, text))
    return out, kept, superseded


def _sentence_start(text, idx):
    before = text[:idx].rstrip()
    return not before or before[-1] in ".!?:\n" or before.endswith(("- ", "-"))


def _term_like(token, at_sentence_start):
    t = token.strip(_PUNCT)
    if len(t) < 2 or not re.search(r"[A-Za-z]", t):
        return None
    upper = sum(c.isupper() for c in t)
    if (upper >= 2 or re.search(r"\d", t) or re.search(r"[A-Za-z][-/][A-Za-z]", t)
            or (upper == 1 and t[0].isupper() and not at_sentence_start)):
        return t
    return None


def diff_corrections(written, current):
    """Word-level corrections between what was written and the edited page.

    Returns a list of ``{"before", "after", "context", "terms"}``. ``terms`` are
    the term-like words the edit introduced (acronyms, CamelCase, names, words
    with digits or an inner hyphen or slash), the raw material of the learned
    vocabulary.
    """
    wa = list(re.finditer(r"\S+", written))
    wb = list(re.finditer(r"\S+", current))
    out = []
    ops = difflib.SequenceMatcher(None, [m.group() for m in wa], [m.group() for m in wb],
                                  autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in ops:
        if op == "equal":
            continue
        before = " ".join(m.group() for m in wa[i1:i2])
        after = " ".join(m.group() for m in wb[j1:j2])
        if _squash([_key(before)]) == _squash([_key(after)]) and not MARK_RE.search(before):
            continue                    # punctuation or spacing only
        before_keys = {_key(m.group()) for m in wa[i1:i2]}
        terms = []
        for m in wb[j1:j2]:
            t = _term_like(m.group(), _sentence_start(current, m.start()))
            if t and _key(t) not in before_keys and t not in terms:
                terms.append(t)
        out.append({"before": before, "after": after,
                    "context": " ".join(m.group() for m in wb[max(0, j1 - 4):j2 + 4]),
                    "terms": terms})
    return out


class LearnedVocab:
    """Terms learned from corrections, with a promotion threshold and an eval gate.

    A term becomes a candidate once it has been corrected in at least
    ``min_count`` different places. Candidates become active either directly
    (no gate) or when an evaluation shows the hint with them is no worse. A term
    that fails the gate is rejected and never retried automatically.
    """

    def __init__(self, path, min_count=2):
        self.path = pathlib.Path(path)
        self.min_count = min_count
        self.data = load_json(self.path, {"terms": {}, "active": [], "rejected": []})

    def record(self, term, where):
        t = self.data["terms"].setdefault(term, {"count": 0, "seen": [], "first": _now()})
        if where not in t["seen"]:
            t["seen"] = (t["seen"] + [where])[-20:]
            t["count"] += 1
        t["last"] = _now()

    def candidates(self):
        done = set(self.data["active"]) | set(self.data["rejected"])
        return sorted(t for t, v in self.data["terms"].items()
                      if v["count"] >= self.min_count and t not in done)

    def active(self):
        return list(self.data["active"])

    def accept(self, terms):
        self.data["active"] += [t for t in terms if t not in self.data["active"]]

    def reject(self, terms):
        self.data["rejected"] += [t for t in terms if t not in self.data["rejected"]]

    def save(self):
        save_json(self.path, self.data)


def goldset_key(source_rel, page):
    return hashlib.sha1(f"{source_rel}#{page}".encode()).hexdigest()[:16]


def write_gold_case(gold_dir, source_rel, page, truth, png_bytes, source_sha256=None,
                    origin="user-edit"):
    """Store one page as ground truth: ``<key>.png`` plus ``<key>.json``.

    A user-edited page is the writer's own reading, though possibly only
    partly corrected, which the ``origin`` field records.
    """
    gold_dir = pathlib.Path(gold_dir)
    gold_dir.mkdir(parents=True, exist_ok=True)
    key = goldset_key(source_rel, page)
    if png_bytes is not None:
        (gold_dir / f"{key}.png").write_bytes(png_bytes)
    save_json(gold_dir / f"{key}.json", {
        "source": source_rel, "page": page, "source_sha256": source_sha256,
        "truth": strip_marks(truth), "origin": origin, "updated": _now(),
        # Flags the editor left alone are still uncertain; the eval runner
        # skips such pages by default rather than score against a guess.
        "unresolved_flags": len(MARK_RE.findall(truth)),
    })
    return key
