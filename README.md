# rm-ocr — reMarkable handwriting OCR

[![CI](https://github.com/delize/remarkable-ocr-handwriting/actions/workflows/ci.yml/badge.svg)](https://github.com/delize/remarkable-ocr-handwriting/actions/workflows/ci.yml)
[![CodeQL](https://github.com/delize/remarkable-ocr-handwriting/actions/workflows/codeql.yml/badge.svg)](https://github.com/delize/remarkable-ocr-handwriting/actions/workflows/codeql.yml)

![](dalle_generated-remarkable-ocr-handwriting.png)

Automatically transcribes any new or changed reMarkable PDF dropped into a
watched directory — into searchable Markdown, **fully local on your device**. No
manual step.

The input side accepts any of **`.pdf`**, **`.zip`**, **`.rmdoc`**, loose
**`.rm`**, or an image (**`.png`**, **`.jpg`**, **`.jpeg`**, **`.webp`**) files
(any mix, in nested folders). PDFs pass through directly; bundles and loose
pages are rendered to PDF via `rmc` first, images are wrapped into a one-page
PDF, and both are cached under `STATE_DIR/rendered/` so a re-extracted-but-
byte-identical source never re-renders. See [Image inputs](#image-inputs) for
what a photo or screenshot goes through. The reference setup uses [Scrybble](https://scrybble.ink) to sync
reMarkable notes into an Obsidian vault, but anything that drops one of those
formats on disk works just as well — `rmapi`/`rmapy` downloads, the reMarkable
desktop app's export folder, a `Syncthing`/`rsync`'d directory, or a manual drop.
Point `VAULT_DIR` + `SOURCE_SUBDIR` at wherever they land. Writing transcripts
into an Obsidian vault is likewise optional; it's just a convenient target
because Obsidian indexes the Markdown for search.

- `rm_ocr.py` — the **proven OCR core** (Qwen3-VL via Ollama). Importable + a CLI.
- `ocr_daemon.py` — the automation: scanner, change-detection manifest, transcript
  writer, and the polling loop. Built *around* the core, not a rewrite of it.
- `rm_render.py` — shared rendering layer: dispatches `.pdf` / `.zip` / `.rmdoc` /
  `.rm` / image inputs to a PDF ready for OCR. Used by both the daemon and the CLI.
- `rm_split.py` — vendored `AUTO_SPLIT` implementation (whitespace-band splitter).
- `selftest.py` — offline test harness (stubs Ollama + poppler + the renderer; zero deps).

## How it works

Three independent pieces: something that **drops reMarkable PDFs** into a folder,
**Ollama** (the model server, run separately), and **rm-ocr** (this poller).

```
 reMarkable ──► Scrybble / rmapi / desktop export ──► PDFs land in the watched folder
                                                              │
                                                              ▼
 ┌─────────────────────────── rm-ocr poll loop (every INTERVAL) ───────────────────────────┐
 │                                                                                          │
 │   for each *.pdf in SOURCE_SUBDIR:                                                        │
 │                                                                                          │
 │   1. recency filter      modified within MAX_AGE_HOURS?            no ─► skip            │
 │   2. change detection    mtime+size moved? then sha256 changed?    no ─► skip (no OCR)   │
 │   3. tall-page handling  AUTO_SPLIT: split in place ─┐                                    │
 │                          REQUIRE_SPLIT: wait ─► pending_split                             │
 │   4. OCR (Ollama)        page images ─► gemma4:26b ─► text   ◄── the only expensive step │
 │   5. write transcript    $OUT_DIR/<mirror>/<stem><OUT_SUFFIX>.md  (frontmatter+backlink) │
 │   6. record in manifest  sha256 + status ─► skipped next pass unless it changes again    │
 │                                                                                          │
 └──────────────────────────────────────────┬───────────────────────────────────────────┘
                                             ▼
                    searchable Markdown transcript (Obsidian indexes it)
```

The funnel is ordered **cheapest-check-first**: a still vault costs microseconds of
`stat()` per file; `sha256` runs only when mtime/size moved; OCR runs only when the
bytes actually changed. See
[deciding before OCR](#not-re-doing-work-how-repeats-are-prevented) for why
re-scanning every cycle stays cheap.

Each pass enumerates source PDFs **modified within `MAX_AGE_HOURS`**, processes the
**new or changed** ones serially, writes one `.md` per PDF into the configurable
`OUT_DIR` tree, then sleeps `INTERVAL`. It is idempotent: unchanged files are
skipped via an `mtime`+`size` pre-filter and an authoritative `sha256`. Editing a
note on the reMarkable and re-syncing re-transcribes only that note. See
[Not re-doing work](#not-re-doing-work-how-repeats-are-prevented) for the full
repeat-prevention design.

## Safety guarantees (enforced in code)

- The vault is mounted **fully read-only** (default); transcripts go to a separate
  `OUT_DIR` volume, so nothing is ever written back into the vault.
- `safe_output_path()` proves every target is a `.md`, never equals the source
  PDF, never lands under a forbidden prefix, and (mirror mode) stays under `OUT_DIR`.
- `assert_safe_paths()` refuses to operate anywhere under any path in
  `FORBIDDEN_PATHS` (env, comma-separated; default `/mnt/docker/scrybble/storage`,
  which is where the standalone Scrybble container keeps its `.rmapi-auth` if
  you run both tools on the same host) — in every mode.
- A malformed PDF logs an error, increments a capped retry counter, and the batch
  continues.

## Prerequisites

rm-ocr is **only the OCR poller** — it does not run Ollama or pull the model for
you. You must already have, separately:

1. **Ollama running** and reachable from the container (default
   `OLLAMA_HOST=http://ollama:11434`). It is its own process/container; `docker
   compose up` for rm-ocr will **not** start it.
2. **The vision model pulled into that Ollama**, once:
   ```bash
   ollama pull gemma4:26b
   ```
   If the model isn't present, the first transcription fails with a
   model-not-found error from Ollama.
3. **reMarkable PDFs landing in a watched directory.** *How* they get there is
   up to you — Scrybble syncing into an Obsidian vault (the reference setup), an
   `rmapi` download script, the reMarkable desktop app's export folder, a synced
   directory, etc. The tool only needs `*.pdf` files under
   `VAULT_DIR/SOURCE_SUBDIR`; it doesn't care what produced them.

In the default Docker setup, rm-ocr reaches Ollama over a shared user-defined
network named `ai` — so the Ollama container must be attached to that network
(see step 2 of the Docker run below). To talk to an Ollama on the host instead,
use the host-port example in [`examples/`](examples/).

## Run it

### Docker (recommended — same network as Ollama)

```bash
# 1. one-time: create the output + state dirs on the host
mkdir -p /mnt/docker/rm-ocr/out /mnt/docker/rm-ocr/state
# (to have Obsidian index transcripts instead, point OUT_DIR at a folder inside the vault)

# 2. make sure Ollama is on a shared user-defined network named `ai`
#    docker network create ai   # if it doesn't exist
#    docker network connect ai ollama

# 3. config + run (compose pulls the prebuilt GHCR image by default)
cp .env.example .env            # edit if needed
docker compose up -d
docker compose logs -f rm-ocr
```

The image is published to **GHCR** by CI on every push to `main` and on version
tags (`vX.Y.Z`): `ghcr.io/delize/remarkable-ocr-handwriting:latest`. It is built
multi-arch (`linux/amd64` + `linux/arm64`). To build locally instead of pulling,
swap the `image:`/`build:` lines in `docker-compose.yml` and run
`docker compose up -d --build`.

**Ready-made compose files** for common setups live in
[`examples/`](examples/) — GHCR-pull (default), local-build, alongside-output
(writable vault), and host-port Ollama. See [`examples/README.md`](examples/README.md).

If you'd rather not create a shared network, reach the published host port instead:
set `OLLAMA_HOST=http://host.docker.internal:11434` and add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the service.

### Host CLI (cron / systemd one-shot)

```bash
pip install -r requirements.txt   # + poppler (brew install poppler / apt install poppler-utils)
                                   # + Inkscape if processing .zip/.rmdoc/.rm (brew install --cask inkscape /
                                   #   apt install inkscape) — plain .pdf input doesn't need it
VAULT_DIR=... OUT_DIR=... STATE_DIR=... python3 ocr_daemon.py --scan   # single incremental pass
python3 ocr_daemon.py --status                                          # manifest summary + any errors
```

Example crontab (hourly, niced):

```
0 * * * * cd /opt/rm-ocr && /usr/bin/nice -n 10 /usr/bin/python3 ocr_daemon.py --scan >> /var/log/rm-ocr.log 2>&1
```

### Direct core CLI (ad-hoc, no manifest)

```bash
python3 rm_ocr.py "/path/to/Vault/remarkable/Work/Sample.pdf" --out ~/ocr_out \
    --model gemma4:26b --threads 14 --no-think
```

## Configuration

All via env (see `.env.example`). The model/inference settings are **settled** —
read the build brief before touching `MODEL`, `NO_THINK`, `THREADS`, or `MAX_PX`.

| Var | Default | Notes |
|---|---|---|
| `VAULT_DIR` | `/vault` | Mounted **read-only** (whole vault) |
| `SOURCE_SUBDIR` | `remarkable` | Subdir of `VAULT_DIR` where the source PDFs land (whatever drops them) |
| `OUT_DIR` | `/out` | **Transcripts output base — its own volume mount.** Mirrors the source subpath under it |
| `OUT_SUBDIR` | `remarkable/_transcripts` | Legacy fallback: used only if `OUT_DIR` is unset (writes inside the vault) |
| `OUT_SUFFIX` | `-handwriting_converted` | Filename = `<source stem><suffix>.md`, e.g. `Sample-handwriting_converted.md` |
| `OUT_ALONGSIDE` | `0` | `1` = write the transcript next to its source PDF (needs a **writable** vault; `OUT_DIR` ignored) |
| `STATE_DIR` | `/state` | Manifest + logs — **must be a persistent volume** |
| `MODEL` | `gemma4:26b` | Vision-capable; larger model, expect slower per-page than a 9B |
| `OLLAMA_HOST` | `http://ollama:11434` | |
| `THREADS` | `14` | cgroup under-detection workaround |
| `NO_THINK` | `1` | Asks the model to skip its reasoning trace. **Some models ignore it** (`qwen3-vl:8b` measured: identical reasoning with and without), so it is a request, not a guarantee — see `NUM_CTX` |
| `SKIP_BLANK_PAGES` | `1` | `1` = skip the OCR call for a genuinely blank page (writes `[blank page]` instead). Small vision models tend to answer blank pages with refusal-style prose otherwise |
| `REFLOW_PARAGRAPHS` | `1` | `1` = join word-wrapped lines into flowing paragraphs. Post-processing on the model's own transcription, not a re-transcription — see [Paragraph reflow](#paragraph-reflow) |
| `DPI` | `150` | Raising alone does nothing (downscaled to `MAX_PX`) |
| `MAX_PX` | `1568` | The real quality/time lever |
| `TIMEOUT` | `1800` | Per-page socket timeout |
| `VISION_CHECK` | `1` | Startup gate: prove the model actually **receives** the images. A runner that drops them makes the model invent a fluent transcript that looks successful. See [The vision gate](#the-vision-gate) |
| `VISION_CHECK_MIN_TOKENS` | `200` | Minimum extra prompt tokens an attached image must cost. A real image costs ~1000+; a dropped one costs a handful |
| `NUM_CTX` | `0` | Model context window in tokens (`0` = Ollama's default of 4096). A page image alone costs ~1800, so a model that reasons first can run out and return **nothing**. **Set `16384` for real handwriting** — dense pages exhaust 4096 even with `IMAGE_AUTOCONTRAST` on. Costs VRAM |
| `MODEL_WAIT_TIMEOUT` | `1800` | Block at startup until the model is loadable on `OLLAMA_HOST`. `0` disables the gate (see [Startup readiness gate](#startup-readiness-gate)) |
| `INTERVAL` | `600` | Poll seconds — the latency floor; an inotify event short-circuits this |
| `INOTIFY` | `1` | `1` = wake immediately on `CLOSE_WRITE` / `MOVED_TO` for any supported input under `SOURCE_SUBDIR` (Linux only; falls back to pure poll if unavailable). See [Inotify wake-up](#inotify-wake-up) |
| `HASH_CHECK` | `1` | `1` = sha256 content detection (authoritative); `0` = last-modified (mtime) detection — cheaper, but re-OCRs on touch-only changes |
| `MAX_AGE_HOURS` | `24` | Only consider PDFs modified within this window; `0` = no limit |
| `MAX_PDF_PAGES` | `0` | Skip documents with more rendered pages than this (`0` = no limit). Counted post-`AUTO_SPLIT`; skipped files show as `SKIPPED` in `--status` and re-queue automatically if the cap is raised |
| `MAX_RETRIES` | `3` | Stop retrying a broken PDF |
| `MIN_REPROCESS_INTERVAL` | `0` | Min seconds between reprocesses of the **same** path even if it changed; `0` = off |
| `RUN_WINDOW` | _(empty)_ | Optional, e.g. `01:00-07:00` |
| `AUTO_SPLIT` | `0` | `1` = split tall PDFs **in place** then OCR, in one pass (see below). Needs `PyMuPDF`+`numpy` and a **writable** source dir |
| `SPLIT_TARGET_PAGE_HEIGHT` | `700` | AUTO_SPLIT: desired output page height (px @ 72dpi) |
| `SPLIT_MIN_GAP_HEIGHT` | `25` | AUTO_SPLIT: smallest whitespace band (px) to cut at |
| `SPLIT_WHITESPACE_THRESHOLD` | `248` | AUTO_SPLIT: row brightness (0–255) counted as whitespace |
| `SPLIT_MAX_SEGMENT_FACTOR` | `2.0` | AUTO_SPLIT: force-cut segments taller than target height x this when no whitespace is found (`0` = never force) |
| `REQUIRE_SPLIT` | `0` | `1` = only OCR PDFs that are split-ready (see below). Needs `pypdf`. For the *external* splitter workflow |
| `SPLIT_MAX_ASPECT` | `2.0` | Page height/width above which a PDF is "too tall" — splits it (AUTO_SPLIT) or holds it (REQUIRE_SPLIT). Match the splitter's `MIN_ASPECT_RATIO` |
| `SPLIT_MARKER_KEY` | `/RemarkableSplitter` | PDF Info-dict key the splitter stamps |
| `SPLIT_MARKER_VALUE` | `processed` | Expected marker value |
| `IMAGE_PAGE_WIDTH_PT` | `445` | Page width, in PDF points, that every image input is normalized to (height follows the aspect ratio). About one reMarkable page, so the `SPLIT_*` tuning applies to photos unchanged. See [Image inputs](#image-inputs) |
| `IMAGE_JPEG_QUALITY` | `92` | Quality of the JPEG embedded in the wrapper PDF. Ignored for bilevel scans, which stay on lossless CCITT |
| `IMAGE_MAX_WIDTH_PX` | `2000` | Downscale image inputs wider than this before embedding (aspect preserved). Guards the decode against a 50 MP phone photo |
| `IMAGE_AUTOCONTRAST` | `1` | Stretch faint ink to true black / paper to true white before embedding. **Not cosmetic** — a faint page can otherwise make a reasoning model transcribe nothing at all. See [Image inputs](#image-inputs) |
| `IMAGE_AUTOCONTRAST_CUTOFF` | `0.5` | Percent of the histogram clipped at each end before stretching. Raise it and genuine light-grey pencil starts getting crushed to white |
| `STROKE_CONTEXT` | `0` | `1` = parse `.rm` stroke geometry into a rough sketch/diagram hint for the OCR prompt + `stroke_regions_flagged` in frontmatter. `.rm`-family sources only; heuristic, not recognition. See [Stroke-assisted OCR context](#stroke-assisted-ocr-context) |
| `DAILY_NOTE_EMBED` | `0` | `1` = after OCR of a date-named source (`YYYY-MM-DD`, or `YYYY-MM-DD-P<n>` for one file per page), ensure the Obsidian daily note embeds the transcript. See [Daily-note embedding](#daily-note-embedding) |
| `DAILY_NOTE_DIR` | `Daily Journal` | Daily-notes folder, relative to `VAULT_DIR`. Must be **outside** `SOURCE_SUBDIR` (refused at startup otherwise) |
| `DAILY_NOTE_HEADING` | `## reMarkable journal` | Heading of the appended section |
| `LOG_LEVEL` | `INFO` | Set `DEBUG` to log each file's gate decision (see below) |

### Where transcripts go (3 modes)

The filename is always `<source stem><OUT_SUFFIX>.md` (default suffix
`-handwriting_converted`), so `Work/Sample.pdf` → `Sample-handwriting_converted.md`
and `Notes/2026-01-01.pdf` → `2026-01-01-handwriting_converted.md`.

| Mode | Set | Result | Vault mount |
|---|---|---|---|
| **Separate base** (default, recommended) | `OUT_DIR=/out` | mirrors source subpath under `/out` | `:ro` |
| **Inside the vault** (Obsidian-indexed) | `OUT_DIR=/vault/_transcripts` (or `OUT_SUBDIR=...`) | mirrors under a vault subfolder | mostly `:ro`, that folder `:rw` |
| **Alongside the source** | `OUT_ALONGSIDE=1` | transcript next to each PDF (`Work/Sample-handwriting_converted.md`) | **`:rw`** |

Alongside mode requires a non-empty `OUT_SUFFIX` (enforced at startup) so a
transcript can never overwrite a source PDF or a Scrybble `.md` stub. The
`/mnt/docker/scrybble/storage` guard stays absolute in every mode.

### Startup readiness gate

Before the first scan, rm-ocr blocks until the model is actually loadable on
`OLLAMA_HOST`:

1. **Presence** — `POST /api/show` is polled with exponential backoff (capped at 30 s);
   404 means "not pulled yet", `URLError` means "ollama unreachable" — each round
   logs the exact failure mode so DNS / port / model-name mistakes surface here
   instead of being masked.
2. **Smoke test** — one `POST /api/generate` with `num_predict=1` to confirm the
   weights actually load (not just that the model is in the catalog).

Without this gate, a cold start that races ahead of `ollama pull` produces a burst
of instant `404`s on `/api/generate`. Those fail in microseconds, so `MAX_RETRIES`
burns in well under a second and every PDF in the first scan ends up flagged as
permanently failed in the manifest — recovery then needs a manual manifest delete.

Tune with `MODEL_WAIT_TIMEOUT` (default `1800` s — generous headroom for a cold
multi-GB pull plus the first CPU model-load). Set `MODEL_WAIT_TIMEOUT=0` to
disable the gate entirely (useful for tests or non-ollama setups).

### Inotify wake-up

The daemon is fundamentally a poller (every `INTERVAL` seconds, scan the source
tree). With `INOTIFY=1` (the default on Linux), a background thread also watches
`SOURCE_SUBDIR` recursively and **sets a wake event** on `CLOSE_WRITE` or
`MOVED_TO` for any `*.pdf`. The main loop's `wait(INTERVAL)` returns immediately,
so a new sync typically starts OCR in seconds rather than waiting out the poll.

The poll keeps running as a correctness floor — if the watcher misses an event
(e.g., the underlying filesystem doesn't propagate inotify, or the watcher thread
dies), the next `INTERVAL` tick catches up. Worst case is identical to today.

Requirements:
- Linux only. `inotify_simple` is `sys_platform == "linux"` in `requirements.txt`;
  on macOS the import fails cleanly and the daemon logs `falling back to pure poll`.
- The backing filesystem must support inotify. **ext4 / btrfs / zfs**: yes.
  **SMB / NFS / FUSE**: typically no — events fire on the server side and don't
  cross the share boundary. The poll covers this transparently.

Set `INOTIFY=0` to skip starting the watcher (useful if the kernel limit
`fs.inotify.max_user_watches` is tight, or for noisy filesystems).

### Tall pages: split then OCR

Some reMarkable exports are a single, *very* tall page (60+ inches). Rasterized and
downscaled to `MAX_PX`, the handwriting collapses into unreadable pixels and OCR
returns garbage. There are **two ways** to handle this — pick one:

**Option A — `AUTO_SPLIT=1` (one tool, recommended).** rm-ocr splits the tall PDF
itself, **in place**, then OCRs the result in the same pass. The split logic is
vendored from
[remarkable-pdf-splitter](https://github.com/delize/remarkable-pdf-splitter)
(whitespace-band detection → ~`SPLIT_TARGET_PAGE_HEIGHT` pages, `/RemarkableSplitter`
marker). The source PDF is **replaced** with the split version (atomic temp +
rename), so the readable split PDF persists *and* gets transcribed. Because the
bytes change, normal change-detection then OCRs the new version. No second
container, no async race.

- Requires the **source dir to be writable** (mount the vault `:rw`, not `:ro`)
  for `.pdf` sources, which are the ones rewritten in place. Bundles and image
  inputs are split on their *cached* render under `STATE_DIR`, so those never
  touch the source and work fine with a `:ro` vault.
- Adds `PyMuPDF` + `numpy`; rm-ocr refuses to start with `AUTO_SPLIT=1` if they're
  missing. Splitting runs on PyMuPDF (each output page references the source page
  once, instead of re-encoding it per segment), so even a native vector export
  with dozens of cuts splits in seconds and the file stays roughly input-sized.
- Content with no detectable whitespace (dark templates, dense sketches) no longer
  passes through uncut: segments taller than `SPLIT_TARGET_PAGE_HEIGHT` x
  `SPLIT_MAX_SEGMENT_FACTOR` (default `2.0`, `0` disables) are subdivided evenly.
- Already-split or short PDFs are left untouched (idempotent via the marker).
- A split failure is recorded as `error` (capped retries) and never aborts the batch.

**Option B — `REQUIRE_SPLIT=1` (external splitter).** Keep splitting in the
standalone tool and have rm-ocr just *wait* for it. See below. Use this if you also
run the splitter for its own sake, or want OCR to stay read-only.

### Split-readiness gate (optional)

Some reMarkable exports are a single, *very* tall page (60+ inches) — the vision
model can't read them. The companion
[remarkable-pdf-splitter](https://github.com/delize/remarkable-pdf-splitter)
breaks those into readable pages and stamps a `/RemarkableSplitter: processed`
marker into the PDF's metadata. Set **`REQUIRE_SPLIT=1`** and rm-ocr will only
transcribe a PDF once it is *split-ready*, meaning **either**:

- it carries the splitter's marker (it has been split), **or**
- no page exceeds `SPLIT_MAX_ASPECT` (height/width) — i.e. it never needed
  splitting in the first place (your "page-height requirements are met" case).

A tall, marker-less PDF is recorded as `pending_split` in the manifest (visible in
`--status`), logged once at INFO, and **left alone** — it consumes no retries and
isn't re-OCR'd. When the splitter later rewrites it (new bytes + marker), the next
pass sees the change and transcribes it. The check reads only the PDF's metadata
and page boxes — far cheaper than an OCR run, and only runs for new/changed files.

This gate is **off by default** (the tool works fine without the splitter) and
requires `pypdf` (already in the image / `requirements.txt`); rm-ocr refuses to
start with `REQUIRE_SPLIT=1` if `pypdf` is missing.

### The vision gate

The daemon refuses to start if the model does not actually **receive** the
images it is sent. This guards the worst failure the tool can have.

Some Ollama runners accept an `images=` payload, silently discard it, and let
the model answer from the text prompt alone. Measured on Ollama 0.32.0's MLX
runner: `gemma4:12b-mlx` was handed a page of handwriting and returned a fluent
essay about 19th-century American industrialisation, repeated verbatim for
pages 1 and 2, written out under `status: ok` with 3416 chars. Nothing about
that transcript looks wrong — which is exactly the problem. **Silent
fabrication in a journal is far worse than a visible failure**, because you have
no reason to doubt it.

The check compares prompt token counts with and without an image attached, so
it tests whether the image *arrives*, not whether the model is any good at
reading it:

| model | no image | with image | verdict |
|---|---|---|---|
| `qwen3-vl:8b` | 24 | **1106** | sees it |
| `gemma4:12b-mlx` | 30 | **35** | drops it |

It costs two 1-token generations at startup. If it can't run (server
unreachable, odd response) it warns and continues rather than blocking startup
on an unrelated fault. `VISION_CHECK=0` disables it, which is not recommended:
the failure it catches is invisible in the output.

Note this is about the *runner*, not the model family — the same model in GGUF
form on the llama.cpp runner handles images normally.

### Image inputs

A `.png`, `.jpg`, `.jpeg` or `.webp` dropped in the source tree is treated as a
photo or screenshot of handwriting. It is wrapped into a one-page PDF by
`rm_render` and then follows the exact same path as everything else, so
`AUTO_SPLIT`, `MAX_PDF_PAGES`, the split gate and the manifest all apply with no
special cases. The wrap runs on Pillow, which `pdf2image` already pulls in, so
image support adds no new dependency.

The wrap does four things worth knowing about:

- **Normalizes the page to `IMAGE_PAGE_WIDTH_PT` (445 pt), height following the
  aspect ratio.** This is the setting that matters. `rm_split` analyses a page at
  one pixel per point, so embedding a 4000 px photo at 1 px = 1 pt would produce
  a 4000 pt wide page and `SPLIT_TARGET_PAGE_HEIGHT` would carve it into slivers.
  445 pt is roughly one reMarkable page (1404 px at 226 dpi), so the existing
  split tuning carries over: a 4:3 photo lands at 445x593 and is never split, a
  long stitched screenshot splits every ~700 pt like a tall notebook export.
- **Applies EXIF rotation.** Phone JPEGs are stored unrotated with an orientation
  tag, so without this the handwriting would reach the model sideways.
- **Flattens transparency onto white.** A plain RGB conversion composites
  transparent pixels onto *black*, which turns a screenshot with a transparent
  background into an unreadable page. Bilevel scans are left on lossless CCITT
  rather than re-encoded as JPEG, which would ring around every pen stroke.
- **Downscales sources wider than `IMAGE_MAX_WIDTH_PX` (2000 px)**, aspect
  preserved, so a 50 MP photo can't blow up the decode on a small container. Tall
  stitched screenshots keep their height, since only the width is capped.
- **Normalizes contrast** (`IMAGE_AUTOCONTRAST`, on by default) so faint pencil
  reaches true black and the paper true white. See below — this one is not
  cosmetic.

#### Why contrast normalization matters more than it sounds

Faint ink doesn't just read worse, it changes how the model behaves. A real
reMarkable page whose darkest pixel was 192 (out of 255) sent `qwen3-vl:8b` into
18k characters of reasoning about ambiguous strokes until it exhausted its
context and returned **nothing at all**. Measured on that page, same model, same
prompt, only the image and context changing:

| image | `num_ctx` | transcript | reasoning | finished? | time |
|---|---|---|---|---|---|
| as-is | 4096 (default) | **0 chars** | 11k | no, hit the limit | — |
| as-is | 16384 | 594 chars | 19k | yes | ~215s |
| **normalized** | **4096** | **621 chars** | **6k** | **yes** | **101s** |
| normalized | 16384 | 621 chars | 6k | yes | 100s |

That is the page's *sparse* first section. Its dense middle section still failed
at 4096 even normalized (9.9k of reasoning, cut off, 0 chars), which is why the
recommendation below is to set both.

Normalizing attacks the cause (two thirds less reasoning, half the wall clock,
no extra VRAM) where `NUM_CTX` only widens the budget the model is burning.

**Use both.** Normalizing is not sufficient on its own: on the same real page,
the sparse first section transcribed fine at the default context, but the dense
middle section still burned 9,866 characters of reasoning and hit the 4096 wall
with nothing to show. Denser handwriting costs more reasoning, so for real
journal pages set **`NUM_CTX=16384`** as well. Contrast lowers the cost; the
context gives the headroom for pages where the lowered cost is still too high.

Pillow's autocontrast is a no-op on already-crisp scans and provably leaves a
blank page blank, so it is safe on by default. Set `IMAGE_AUTOCONTRAST=0` to
keep the original tones.

Note the contrast step applies to **image inputs only** — a faint `.pdf` or
`.rm` bundle does not pass through the wrap, so those depend on `NUM_CTX` alone.
Either way the failure is now loud: a page that returns nothing is marked in the
transcript with the reason, and a document where every page returns nothing is
recorded as an error rather than a plausible-looking empty file.

Two things to watch for:

- **Resolution.** OCR rasterizes at `DPI` (default `150`), so a 445 pt page
  becomes only ~927 px wide no matter how sharp the original photo was. For
  photographed handwriting set **`DPI=254`**, which lands at ~1570 px, right at
  the `MAX_PX` cap of 1568.
- **Same-stem collisions.** `note.png` and `note.pdf` in one folder both want
  `note-handwriting_converted.md`. The second one transcribed gets
  `-<source_sha256[:8]>` appended, so neither overwrites the other.

Stroke-context hints are never available for images: a photo carries no vector
ink, so `page_regions` is always empty regardless of `STROKE_CONTEXT`. HEIC is
not supported (it needs `pillow-heif`); convert to JPEG first.

### Stroke-assisted OCR context

`STROKE_CONTEXT=1` (default off) parses each source `.rm` page's vector stroke
geometry — the pen-tool and point data `rmscene` exposes for `.rm`/`.rmdoc`/`.zip`
inputs — and clusters it into a rough "this region is probably a sketch, diagram,
or drawing" hint. If a page has one, a short sentence is appended to that page's
OCR prompt (transcribe handwriting normally; describe flagged regions in
`[brackets]` instead of guessing at exact wording), and the count is recorded in
the transcript's frontmatter as `stroke_regions_flagged`.

**What this is not:** real handwriting recognition. `rmscene`/`rmc` expose raw
ink geometry (tool id + per-point x/y/pressure/etc.) with no text/drawing
label, and neither library does any ink-to-text conversion — confirmed by
reading `rmc`'s own `markdown` exporter, which only extracts *typed* keyboard
text and highlighter ranges over already-digital text. The mature engines that
do turn strokes into text (reMarkable's own "Convert to text", MyScript iink,
Azure Ink Recognizer) are all proprietary cloud services, which would break
this project's fully-local guarantee — so `STROKE_CONTEXT` stays a local,
offline, size/shape heuristic: it will misfire on compact diagrams and on
effusive handwriting. Treat the hint and the frontmatter count as signals, not facts.

Scope and interactions:

- **`.rm`-family sources only.** A plain `.pdf` input never carries stroke
  data, so `STROKE_CONTEXT` has no effect on it.
- **Needs `rmscene`** (normally already present — it's a transitive dep of
  `rmc`); rm-ocr refuses to start with `STROKE_CONTEXT=1` if it's missing.
- **`AUTO_SPLIT` interaction:** stroke regions are computed per *original*
  `.rm` page. If `AUTO_SPLIT` actually re-splits a document's rendered pages,
  the region-to-page mapping would no longer line up, so rm-ocr drops the
  hints for that document rather than risk attaching one to the wrong page.
  `REQUIRE_SPLIT` doesn't change page count, so it has no such interaction.
- **Render cache:** the region data for a bundle/`.rm` is cached alongside its
  rendered PDF (`STATE_DIR/rendered/<sha>.regions.json`), so a cache hit
  doesn't need to re-parse the source.

The CLI has the equivalent `--stroke-context` flag.

### Daily-note embedding

`DAILY_NOTE_EMBED=1` (default off) closes the loop for daily journals kept on
the tablet: after a source whose title is a plain date (`2026-07-20.pdf`) is
transcribed, the daemon ensures the matching Obsidian daily note
`<vault>/<DAILY_NOTE_DIR>/2026-07-20.md` contains a section embedding the
transcript:

```markdown
## reMarkable journal

![[remarkable/Daily Journal/2026-07-20-hwr]]
```

It is transclusion, not copying — the note gets one embed line pointing at the
transcript's **full vault-relative path**, and Obsidian renders the current
transcript content inline. When you update the page on the tablet and it gets
re-OCR'd, the note is already up to date; no second write happens.

Safety properties, since this is the one feature that touches human-edited
files:

- **Append-only.** Existing prose is never rewritten; the section is appended
  once at the end. Writes go through a temp file + atomic rename, so a crash
  can never truncate a note.
- **Multi-page days.** A day exported one file per page — `2026-07-02-P001`,
  `-P002`, ... — keeps a separate transcript per page but embeds them all into
  the single `2026-07-02.md`, under **one** heading, in page order. Anything you
  wrote after that section stays where it is.
- **Idempotent.** A note that already references the transcript path — this
  section, or a link you wrote yourself — is left alone.
- **Full-path embeds.** `2026-07-20.md` often exists twice in a vault (the
  daily note and a sync-tool stub next to the PDF); a bare basename embed
  would be ambiguous, so the full path is always used.
- **Missing notes are created** with just the section, so a transcript synced
  before Obsidian first opens that day still lands.
- **Config guard.** `DAILY_NOTE_DIR` inside `SOURCE_SUBDIR` is refused at
  startup — date-named `.md` files in the source tree belong to the sync tool
  and would be clobbered on its next sync.
- **Scope guard.** Only titles matching `YYYY-MM-DD` or `YYYY-MM-DD-P<n>`
  participate; everything else is untouched. A stem like `2026-07-23-groceries`
  is *not* a daily page and never creates a note. An embed failure is logged and never fails or retries the
  completed transcription.

Requirements: the vault mounted **writable** (like `OUT_ALONGSIDE`), and
transcripts landing **inside** the vault (`OUT_ALONGSIDE=1`, or `OUT_DIR`
under the vault mount) — Obsidian can't transclude a file outside the vault,
so with an external `OUT_DIR` the embed is skipped with a warning.

### Paragraph reflow

Handwriting wraps at the edge of the page, not at the end of a sentence, so a
literal transcription reads as one short, choppy line per physical line on the
page. `REFLOW_PARAGRAPHS=1` (default on) joins those word-wrapped lines back
into flowing paragraphs, as a **text post-processing step on the model's own
output** — headings, bullet/numbered lists, blockquotes, and fenced code
blocks (including any blank line inside one) are left exactly as the model
wrote them; only plain prose lines get joined with a space. A blank line
between two blocks of text is treated as a real paragraph break and preserved.

This is deliberately *not* done by asking the model to reflow while
transcribing. That was tried first and rejected: even a carefully-worded
prompt ("don't paraphrase, just join wrapped lines") measurably pushed a local
vision model toward generating plausible-sounding prose instead of
transcribing faithfully — on one real test page it fabricated several
paragraphs of generic text with no relationship to the actual handwriting.
Reflowing after the fact operates only on text the model already produced, so
it carries no risk of introducing new hallucinated content — at most it can
badly reflow, never invent.

Set `REFLOW_PARAGRAPHS=0` (or `--no-reflow` on the CLI) to keep the model's
literal per-page-line output instead.

### Not re-doing work: how repeats are prevented

Three independent layers, so the same page is never transcribed twice unless it
genuinely changed:

1. **Recency window (`MAX_AGE_HOURS`)** — each pass only looks at PDFs whose mtime
   is within the window. Old, already-handled notes aren't even statted, and a
   first run doesn't transcribe the whole backlog. (Run once with
   `MAX_AGE_HOURS=0` to deliberately backfill.)
2. **Persistent manifest + content hash** — every processed file is recorded in
   `STATE_DIR/manifest.json` with its `sha256`. A cheap `mtime`+`size` pre-filter
   skips untouched files instantly; the `sha256` is the authoritative check, so a
   Scrybble re-sync that rewrites a byte-identical PDF (new mtime, same bytes) is
   skipped. **This only works if `STATE_DIR` is a persistent volume** — if state
   is lost, everything looks new and gets redone. Failures are capped at
   `MAX_RETRIES` so a broken PDF can't be retried forever.
3. **Cooldown (`MIN_REPROCESS_INTERVAL`, optional)** — the one case the hash can't
   catch is a source that re-renders the *same* note to *different* bytes every
   sync (non-deterministic PDF). The cooldown refuses to reprocess a given path
   more than once per N seconds regardless, breaking that loop. Off by default;
   set e.g. `3600` if you ever see a note re-transcribing every cycle.

The output is also written to a **separate `OUT_DIR` volume by default, not back
into the vault**, so transcripts can never be mistaken for new source PDFs (no
feedback loop) and the vault stays fully read-only.

**The decision happens before OCR — and is cheap.** Per file, per pass:

| Stage | Cost | Runs when |
|---|---|---|
| `stat()` mtime+size compare | microseconds | every file |
| sha256 hash | milliseconds (disk read, ~no CPU) | only if mtime **or** size moved |
| OCR (`ocr_pdf`) | ~1 min/page, CPU-pegged | only if the content token is new/changed |

OCR is never run to *decide* anything — `needs_work()` gates first; only a
`queued` verdict reaches the model. A still vault stops every file at the `stat`
compare (no hashing). Set `LOG_LEVEL=DEBUG` to watch it:

```
gate=prefilter-skip  Work/Sample.pdf  (mtime+size unchanged, no hash, no OCR)
gate=hash-unchanged  Work/Sample.pdf  (touched but bytes identical, no OCR)
gate=retry-capped    Work/Bad.pdf    (errored 3 times, no OCR)
gate=queued          Work/Sample.pdf  (changed -> will OCR)
```

## Dependencies

Plain Python with a small set of pip + system deps, all baked into the image:

- **`pdf2image`** (pip — see `requirements.txt`; pulls in Pillow) + **poppler**
  (system: `apt-get install poppler-utils` / `brew install poppler`). Poppler also
  provides `pdfunite`, used to merge per-page renders into a single bundle PDF.
  Pillow additionally decodes image inputs and writes their one-page wrapper PDF,
  so `.png`/`.jpg`/`.jpeg`/`.webp` support needs nothing beyond what is already
  here (in particular, not PyMuPDF).
- **`rmc`** (pip; pulls in `rmscene`) — renders `.zip` / `.rmdoc` / `.rm` inputs
  to PDF. Its PDF export shells out to **Inkscape** (system: `apt-get install
  inkscape` / `brew install --cask inkscape`) to rasterize an intermediate SVG
  — no Chrome or cairo involved. Pure-PDF workflows ignore both entirely.
  `rmscene` is also declared directly (`rm_strokes.py` imports it for
  `STROKE_CONTEXT`, lazily).
- **`PyMuPDF` + `numpy`** — used only by `AUTO_SPLIT` (lazy-imported; rm-ocr
  refuses to start with `AUTO_SPLIT=1` if they're missing). PyMuPDF both renders
  tall pages for the whitespace analysis and assembles the split output; note it
  is AGPL-3.0 licensed.
- **`pypdf`** — used only by the `REQUIRE_SPLIT` gate (lazy-imported).
- **`inotify_simple`** (Linux only) — opt-in wake-up signal layered on top of
  the poll; gracefully no-ops on macOS.
- **Ollama** running with the model pulled: `ollama pull gemma4:26b`.

`selftest.py` stubs `pdf2image`, the OCR call, and the renderer, so it runs with
**no dependencies at all** — even rmc — via `python3 selftest.py`.

**Forcing a re-render after an `rmc` upgrade.** The render cache key is the
source bundle's bytes, so an `rmc` version bump does *not* invalidate cached
PDFs. If you want a clean re-render after a deliberate `rmc` upgrade, wipe the
cache:

```bash
docker compose down                       # or just stop the container
rm -rf /mnt/docker/rm-ocr/state/rendered  # wherever your STATE volume lives
docker compose up -d
```

The manifest is untouched, so the next scan re-renders each bundle and re-OCRs
it cleanly.

## Transcript format

```markdown
---
source: remarkable/Work/Sample.pdf
model: gemma4:26b
source_modified: 2026-05-30T09:14:02
processed_at: 2026-05-30T12:00:00
pages: 3
chars_per_page: [812, 640, 91]
stroke_regions_flagged: 1
status: ok
---

# Sample

Source: [[remarkable/Work/Sample.pdf]]

## Page 1

...transcription...
```

`stroke_regions_flagged` only appears when `STROKE_CONTEXT=1` (see
[Stroke-assisted OCR context](#stroke-assisted-ocr-context)); it's the total
count of probable sketch/diagram regions across all pages, not a per-page
breakdown.

## State

`STATE_DIR/manifest.json` keyed by vault-relative source path:
`{ mtime, size, sha256, source_modified, out_path, pages, chars_per_page,
processed_at, status, retries, render_sha256? }`. Written atomically (temp file
+ rename). `STATE_DIR/ocr.log` mirrors stdout.

- `sha256` is always the **source bytes** hash — for `.pdf` that's the PDF, for
  bundles that's the `.zip`/`.rmdoc`/`.rm`, for an image input that's the
  original `.png`/`.jpg`/`.jpeg`/`.webp`. It's the change-detection token.
- `render_sha256` is set for rendered inputs only — the hash of the cached PDF
  under `STATE_DIR/rendered/<sha[:2]>/<sha>.pdf`. Useful for tracing which
  rendered output produced a transcript.
- With `STROKE_CONTEXT=1`, a sibling `STATE_DIR/rendered/<sha[:2]>/<sha>.regions.json`
  holds that render's stroke-region data, so a cache hit doesn't need to
  re-parse the source.

`STATE_DIR/rendered/` is the render cache. Sharded two levels deep
(`<sha[:2]>/<sha>.pdf`). Keyed by source bytes, so renaming a bundle is a free
cache hit and an `rmc` upgrade is *not* a cache invalidation (see the
re-render tip in Dependencies if you want one).
