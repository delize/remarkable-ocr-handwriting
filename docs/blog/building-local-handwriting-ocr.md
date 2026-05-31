---
title: "Transcribing reMarkable handwriting to Obsidian — fully local, on a GPU-less NAS"
date: 2026-05-31
tags: [reMarkable, obsidian, ocr, ollama, homelab, llm, self-hosted]
---

# Transcribing reMarkable handwriting to Obsidian — fully local, on a GPU-less NAS

I write a lot by hand on a reMarkable. [Scrybble](https://scrybble.ink) already
syncs those notes into my Obsidian vault as rendered PDFs — which is great for
*reading*, but a PDF of my handwriting is invisible to search. I wanted the
words themselves: searchable, linkable, greppable Markdown. And I wanted it to
happen **automatically, entirely on my own hardware**, with no note ever leaving
the house.

This is the story of building that — the models I tried and threw away, the two
settings that made the difference between *67 seconds* and *27 minutes* per page,
how accurate a local model actually is on messy handwriting, and why the final
design is a humble polling loop instead of something cleverer.

The result is [`rm-ocr`](https://github.com/delize/scrybble-ocr-handwriting): a
small containerized daemon that watches the vault and transcribes any new or
changed note.

---

## The constraint that shaped everything: no GPU

The hardware is a NAS — an Intel i5-13600K (14 cores / 20 threads), 96 GB of RAM,
and **no discrete GPU**. That single fact dictates the entire design. Every model
decision below is really a decision about *what can run at a tolerable speed on a
CPU*.

Handwriting OCR with a modern model is a **vision-language** task: the model
looks at a rasterized image of the page and emits text. That means two
phases per page:

1. **Prefill** — the model ingests the image (hundreds to thousands of image
   tokens). This is compute-heavy and, on a CPU, it's where the time goes.
2. **Decode** — the model emits the transcription token by token.

On a GPU, prefill is trivial. On a CPU, prefill *is* the cost. Hold that thought —
it explains why several "obvious" optimizations turned out to be dead ends.

---

## Why we tested different models (and what each one taught us)

The goal was the best transcription quality that still finishes a page in a
reasonable time on CPU. "Reasonable" for a background daemon means roughly a
minute or two per page — slow is fine, *stuck* is not.

### Big models: the MoE mirage

The intuition is "bigger model = better reading," so the first instinct is to
reach for the large vision models: `qwen3.5:27b`, `qwen3-vl:30b`, and the
mixture-of-experts (`*-a3b`) variants.

On this box, **every model in the ~17 GB-and-up class took 30+ minutes per
page.** Unusable for an automated pipeline.

The MoE variants are the interesting trap. The pitch for mixture-of-experts is
"only a fraction of the parameters activate per token, so it's cheap." That's
true *for decode*. But OCR is **prefill-bound**, and during prefill — processing
that big page image — most experts end up activated anyway. The thing that makes
MoE fast is exactly the thing OCR doesn't lean on. So MoE bought us nothing and
cost us the larger memory footprint. Lesson learned empirically, not from a spec
sheet.

### MLX: wrong platform, wrong modality

Apple's MLX-format models look tempting for fast local inference — but they're
**Apple-Silicon only**, and the variants available were **text-only** (no image
input). On an Intel NAS doing vision OCR, doubly useless. Ruled out.

### The sweet spot: a ~9B vision model

That left the ~9B class. `qwen3.5:9b` (the default q4_K_M quant, ~6.6 GB) is
vision-capable, fits comfortably in RAM, and — once tuned (next section) — does a
**neat page in about 67 seconds**. Messy pages run longer but stay readable.

This is the quality/speed knee of the curve on CPU. Below it, accuracy drops off;
above it, you're paying minutes-to-tens-of-minutes per page for diminishing
returns you can't afford on a background job.

### The settled decision

| Model class | Result on this NAS | Verdict |
|---|---|---|
| ≥17 GB (27b, 30b, `*-a3b` MoE) | 30+ min/page | ❌ Dead end — MoE doesn't help prefill |
| MLX variants | Apple-only + text-only | ❌ Wrong platform/modality |
| **`qwen3.5:9b` (q4_K_M)** | **~67 s/page neat** | ✅ **Chosen** |
| `qwen3.5:9b-q8_0` (~11 GB) | ~1.5–2× slower | ⚠️ Optional, for the hard subset only |

---

## The two issues that made or broke it

Picking the model was maybe a third of the battle. The rest was discovering — the
hard way — two configuration problems that each turned a working setup into a
broken one.

### Issue #1: the "thinking" trap — 27 minutes and *empty output*

Qwen3.5 is a *reasoning* ("thinking") model: by default it emits an internal
chain-of-thought before its answer. For most tasks that's a feature. For OCR it
was a catastrophe.

With thinking left **on**, a single page took **27 minutes and produced empty
output.** The model spent its entire context budget "reasoning" about the image —
narrating what it saw, second-guessing strokes — and ran out of room before it
ever transcribed anything.

The fix is one flag: `think: false` in the Ollama request.

```python
payload = { "model": model, "prompt": PROMPT, "images": [...], "think": False }
```

That single setting is the difference between **67 seconds** and **unusable**.
OCR doesn't want deliberation; it wants a direct transcription. We tell the model
exactly that, and turn the reasoning machinery off.

### Issue #2: the silent under-threading — Docker, cgroups, and idle E-cores

Ollama running inside Docker mis-reads the container's CPU quota. The cgroup v2
`cpu.max` value comes back as the string `"max"` (meaning "unlimited"), Ollama
fails to parse it into a core count, and **silently falls back to using only 6
threads** — leaving the 13600K's efficiency cores sitting idle while a page
crawls through prefill.

The fix is to force the thread count explicitly:

```python
opts = {"num_thread": 14}   # match the physical cores; don't trust autodetect
```

Forcing 14 threads roughly **halved prefill time**. Nothing in the logs warns you
about this; you only notice if you watch CPU utilization and see it pinned at ~6
cores' worth instead of saturating the chip.

### The DPI red herring

A third, smaller gotcha worth recording: cranking the render **DPI** does
nothing for quality. The page image is downscaled to `max_px` (default 1568 on
its longest edge) *before* the model ever sees it — so a 300-DPI render and a
150-DPI render collapse to the same input. The real quality/time lever is
**`max_px`**, and it scales with *area*: ~2048 px is about 2 min/page, ~3072 px
about 4–5 min/page. Raise it only for the genuinely hard pages; spending it on
neat daily notes is pure waste.

---

## How accurate is a local 9B model, really?

Honest answer: **a readable transcript with occasional hard-token errors.**

The model gets the overwhelming majority of words right and preserves layout and
line breaks well. Where it slips is on individual ambiguous tokens — the kind a
human also has to squint at. A representative real error:

> "Only 6 days" → "Guy 6 pays"

You can see what happened: the shapes are plausible, the model committed to the
wrong reading. It's not gibberish — it's a confident misread of a hard token.

Some calibration:

- **A frontier vision model (e.g. Claude) is a clear tier above** on the same
  pages. But it isn't local, and "local" was a hard requirement here. This is a
  deliberate trade: privacy and zero marginal cost in exchange for the occasional
  wrong word.
- For neat writing, it's good enough to **make notes searchable and skimmable**,
  which was the actual goal — not a flawless legal transcript.
- For the hard subset, there are two opt-in levers: the `q8_0` quant (~1.5–2×
  slower) and/or a higher `max_px`. Crucially, you apply those **only** to the
  pages that need them — never to the neat dailies, where they'd just burn CPU.

To keep transcription faithful, the prompt is deliberately minimal: transcribe
exactly, preserve layout, and write `[illegible]` rather than *guess* when a word
truly can't be read. The journals are personal; faithful transcription is the
only default behavior. (Any "summarize / tag / extract action items" enrichment
is left opt-in and off.)

A future discrete GPU changes this calculus entirely — a used 24 GB card runs the
27b model at *seconds* per page, which would lift quality a tier while getting
*faster*. That's a hardware upgrade, out of scope for v1, but the pipeline is
ready for it: it's just a model name and a config change.

---

## Why we poll and wait instead of reacting instantly

The daemon's core loop is almost aggressively boring:

> scan the vault → transcribe anything new or changed → sleep `INTERVAL` (default
> 600 s) → repeat.

Why a timer instead of an instant file-watcher (inotify) or a webhook from the
sync? Several reasons, and they all point the same way.

**1. The work is serial and slow by nature.** OCR pegs the CPU and Ollama is
configured for one request at a time (`NUM_PARALLEL=1`). Running two pages at
once just makes both slower. So even if ten notes landed simultaneously, we'd
still process them one after another. An event storm gains you nothing when the
worker is single-file by design — a calm periodic drain is the honest model of
what's actually happening.

**2. Politeness to the rest of the homelab.** This NAS also runs Plex and other
containers. A pipeline that pounces the instant a file appears can collide with a
movie someone's watching. Polling on an interval — optionally constrained to a
quiet window like 01:00–07:00, optionally `nice`-d — keeps OCR a good neighbor.

**3. The sync is eventually-consistent anyway.** Scrybble renders and drops PDFs
on its own schedule; a note "appears" as a write that may itself be one of
several. Waiting a few minutes and then reconciling the whole tree is far more
robust than trying to fire on each individual filesystem event and guess when a
file is "done."

**4. Determinism and recovery.** A poll loop has no missed-event problem. If the
daemon was down, the box rebooted, or a pass crashed on one bad PDF, the *next*
scan simply picks up whatever's outstanding. There's no event queue to lose, no
webhook to redeliver. State lives in a manifest on disk; each pass is a
self-contained reconciliation.

### The part that makes polling cheap: deciding *before* OCR

The obvious objection to "re-scan everything every 10 minutes" is: *doesn't that
re-OCR the whole vault constantly?* No — and this was a specific design point.
**OCR is never run to decide whether OCR is needed.** Each file passes through a
cheap gate first:

| Stage | Cost | Runs when |
|---|---|---|
| `stat()` — compare mtime + size | microseconds | every file, every pass |
| `sha256` of the PDF | milliseconds (a disk read) | only if mtime or size moved |
| **OCR** | ~1 min/page, CPU-pegged | only if the content actually changed |

A vault that hasn't changed since the last pass stops every file at stage 1 —
microseconds of `stat()` calls, then back to sleep. A file only gets hashed if
its timestamp or size moved, and only gets transcribed if the **bytes genuinely
changed**. Set `LOG_LEVEL=DEBUG` and you can watch each file's verdict:
`prefilter-skip`, `hash-unchanged`, `retry-capped`, or `queued`.

### Why hash the rendered PDF instead of asking the cloud?

reMarkable's cloud API (via `rmapi`) *can* tell you a document's last-modified
time and a monotonic `version` number. It's tempting to use that as the change
signal. We deliberately don't, for two reasons:

- **The PDF is what we transcribe.** If the locally-rendered PDF is
  byte-identical, there is nothing new to OCR no matter what the cloud says. The
  cloud `version` bumps on metadata-only edits too — a rename, a re-tag, a move —
  none of which change a single pen stroke. Keying off it would trigger a wasted
  5-minute OCR for a note that didn't visually change. The local content hash is
  simpler and *more correct*.
- **No second credential.** Scrybble already holds the reMarkable auth token.
  Wiring `rmapi` into the OCR service would mean a second authenticated cloud
  client and a second copy of that credential to guard. The pipeline never reads,
  mounts, or touches the credential store — by design.

There's one residual edge — a note that re-renders to *different bytes* without
any visual change (say, an embedded timestamp in the PDF). For that there's an
optional cooldown (`MIN_REPROCESS_INTERVAL`) that refuses to reprocess the same
path more than once per N seconds. Off by default; flip it on only if you ever
observe a note re-transcribing for no visible reason.

---

## Putting it together

The finished pipeline is small and deliberately unexciting:

- A **read-only** mount of the Obsidian vault (so it can never harm the source).
- Transcripts written to a separate, configurable location as one `.md` per PDF,
  with frontmatter (`source`, `model`, `source_modified`, `pages`, …) and an
  Obsidian backlink, mirroring the source folder layout.
- A manifest on disk for idempotent change detection.
- A poll loop that drains new work serially, politely, and recoverably.
- Shipped as a multi-arch container image, published by CI, with a self-test that
  exercises the whole decision path without needing Ollama or a real PDF.

None of the individual pieces are clever. The value was in the *empirical* parts:
discovering that MoE doesn't help prefill, that one `think: false` flag is worth
26 minutes a page, that a Docker cgroup quirk silently halves your throughput,
and that the right change signal is a hash of the local render — not a cloud
query. Those are the kind of things you only learn by running the thing on the
actual hardware and watching where the minutes go.

The notebook is now searchable. That was the whole point.

---

*Code: [github.com/delize/scrybble-ocr-handwriting](https://github.com/delize/scrybble-ocr-handwriting)
· Image: `ghcr.io/delize/scrybble-ocr-handwriting`*
