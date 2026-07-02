# rm-ocr — reMarkable handwriting OCR

[![CI](https://github.com/delize/remarkable-ocr-handwriting/actions/workflows/ci.yml/badge.svg)](https://github.com/delize/remarkable-ocr-handwriting/actions/workflows/ci.yml)
[![CodeQL](https://github.com/delize/remarkable-ocr-handwriting/actions/workflows/codeql.yml/badge.svg)](https://github.com/delize/remarkable-ocr-handwriting/actions/workflows/codeql.yml)

Automatically transcribes any new or changed reMarkable PDF dropped into a
watched directory — into searchable Markdown, **fully local on your device**. No
manual step.

The input side accepts any of **`.pdf`**, **`.zip`**, **`.rmdoc`**, or loose
**`.rm`** files (any mix, in nested folders). PDFs pass through directly;
bundles and loose pages are rendered to PDF via `rmc` first and cached under
`STATE_DIR/rendered/` so a re-extracted-but-byte-identical bundle never re-
renders. The reference setup uses [Scrybble](https://scrybble.ink) to sync
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
  `.rm` inputs to a PDF ready for OCR. Used by both the daemon and the CLI.
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
| `NO_THINK` | `1` | **Required** — thinking ON = unusable |
| `DPI` | `150` | Raising alone does nothing (downscaled to `MAX_PX`) |
| `MAX_PX` | `1568` | The real quality/time lever |
| `TIMEOUT` | `1800` | Per-page socket timeout |
| `MODEL_WAIT_TIMEOUT` | `1800` | Block at startup until the model is loadable on `OLLAMA_HOST`. `0` disables the gate (see [Startup readiness gate](#startup-readiness-gate)) |
| `INTERVAL` | `600` | Poll seconds — the latency floor; an inotify event short-circuits this |
| `INOTIFY` | `1` | `1` = wake immediately on `CLOSE_WRITE` / `MOVED_TO` for `*.pdf` under `SOURCE_SUBDIR` (Linux only; falls back to pure poll if unavailable). See [Inotify wake-up](#inotify-wake-up) |
| `HASH_CHECK` | `1` | `1` = sha256 content detection (authoritative); `0` = last-modified (mtime) detection — cheaper, but re-OCRs on touch-only changes |
| `MAX_AGE_HOURS` | `24` | Only consider PDFs modified within this window; `0` = no limit |
| `MAX_RETRIES` | `3` | Stop retrying a broken PDF |
| `MIN_REPROCESS_INTERVAL` | `0` | Min seconds between reprocesses of the **same** path even if it changed; `0` = off |
| `RUN_WINDOW` | _(empty)_ | Optional, e.g. `01:00-07:00` |
| `AUTO_SPLIT` | `0` | `1` = split tall PDFs **in place** then OCR, in one pass (see below). Needs `pypdf`+`numpy`+Pillow and a **writable** source dir |
| `SPLIT_TARGET_PAGE_HEIGHT` | `700` | AUTO_SPLIT: desired output page height (px @ 72dpi) |
| `SPLIT_MIN_GAP_HEIGHT` | `25` | AUTO_SPLIT: smallest whitespace band (px) to cut at |
| `SPLIT_WHITESPACE_THRESHOLD` | `248` | AUTO_SPLIT: row brightness (0–255) counted as whitespace |
| `REQUIRE_SPLIT` | `0` | `1` = only OCR PDFs that are split-ready (see below). Needs `pypdf`. For the *external* splitter workflow |
| `SPLIT_MAX_ASPECT` | `2.0` | Page height/width above which a PDF is "too tall" — splits it (AUTO_SPLIT) or holds it (REQUIRE_SPLIT). Match the splitter's `MIN_ASPECT_RATIO` |
| `SPLIT_MARKER_KEY` | `/RemarkableSplitter` | PDF Info-dict key the splitter stamps |
| `SPLIT_MARKER_VALUE` | `processed` | Expected marker value |
| `STROKE_CONTEXT` | `0` | `1` = parse `.rm` stroke geometry into a rough sketch/diagram hint for the OCR prompt + `stroke_regions_flagged` in frontmatter. `.rm`-family sources only; heuristic, not recognition. See [Stroke-assisted OCR context](#stroke-assisted-ocr-context) |
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

- Requires the **source dir to be writable** (mount the vault `:rw`, not `:ro`).
- Adds `pypdf` + `numpy` (+ Pillow, already present); rm-ocr refuses to start with
  `AUTO_SPLIT=1` if they're missing.
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
- **`rmc`** (pip; pulls in `rmscene`) — renders `.zip` / `.rmdoc` / `.rm` inputs
  to PDF. Its PDF export shells out to **Inkscape** (system: `apt-get install
  inkscape` / `brew install --cask inkscape`) to rasterize an intermediate SVG
  — no Chrome or cairo involved. Pure-PDF workflows ignore both entirely.
  `rmscene` is also declared directly (`rm_strokes.py` imports it for
  `STROKE_CONTEXT`, lazily).
- **`pypdf` + `numpy`** — used only by `AUTO_SPLIT` (lazy-imported; rm-ocr refuses
  to start with `AUTO_SPLIT=1` if they're missing).
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
  bundles that's the `.zip`/`.rmdoc`/`.rm`. It's the change-detection token.
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
