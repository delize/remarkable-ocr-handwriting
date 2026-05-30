# rm-ocr — reMarkable → Obsidian handwriting OCR

Automatically transcribes any new or changed reMarkable PDF (synced into the
Obsidian vault by Scrybble) into searchable Markdown, **fully local on the NAS**.
No manual step.

- `rm_ocr.py` — the **proven OCR core** (Qwen3-VL via Ollama). Importable + a CLI.
- `ocr_daemon.py` — the automation: scanner, change-detection manifest, transcript
  writer, and the polling loop. Built *around* the core, not a rewrite of it.
- `selftest.py` — offline test harness (stubs Ollama + poppler; zero deps).

## How it works

```
vault/remarkable/**/*.pdf  ──scan (last 24h)──▶  manifest (sha256 diff)  ──▶  ocr_pdf()  ──▶
    $OUT_DIR/<mirror>/<stem><OUT_SUFFIX>.md      (frontmatter + backlink)
```

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
- `assert_safe_paths()` + `FORBIDDEN_PREFIXES` refuse to operate anywhere under
  `/mnt/docker/scrybble/storage` (the `.rmapi-auth` credential lives there) — in
  every mode.
- A malformed PDF logs an error, increments a capped retry counter, and the batch
  continues.

## Run it

### Docker (recommended — same network as Ollama)

```bash
# 1. one-time: create the output + state dirs on the host
mkdir -p /mnt/docker/rm-ocr/out /mnt/docker/rm-ocr/state
# (to have Obsidian index transcripts instead, point OUT_DIR at a folder inside the vault)

# 2. make sure Ollama is on a shared user-defined network named `ai`
#    docker network create ai   # if it doesn't exist
#    docker network connect ai ollama

# 3. config + build + run
cp .env.example .env            # edit if needed
docker compose up -d --build
docker compose logs -f rm-ocr
```

If you'd rather not create a shared network, reach the published host port instead:
set `OLLAMA_HOST=http://host.docker.internal:11434` and add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the service.

### Host CLI (cron / systemd one-shot)

```bash
pip install -r requirements.txt   # + poppler (brew install poppler / apt install poppler-utils)
VAULT_DIR=... OUT_DIR=... STATE_DIR=... python3 ocr_daemon.py --scan   # single incremental pass
python3 ocr_daemon.py --status                                          # manifest summary + any errors
```

Example crontab (hourly, niced):

```
0 * * * * cd /opt/rm-ocr && /usr/bin/nice -n 10 /usr/bin/python3 ocr_daemon.py --scan >> /var/log/rm-ocr.log 2>&1
```

### Direct core CLI (ad-hoc, no manifest)

```bash
python3 rm_ocr.py "/path/to/Vault/remarkable/Work/Carol.pdf" --out ~/ocr_out \
    --model qwen3.5:9b --threads 14 --no-think
```

## Configuration

All via env (see `.env.example`). The model/inference settings are **settled** —
read the build brief before touching `MODEL`, `NO_THINK`, `THREADS`, or `MAX_PX`.

| Var | Default | Notes |
|---|---|---|
| `VAULT_DIR` | `/vault` | Mounted **read-only** (whole vault) |
| `SOURCE_SUBDIR` | `remarkable` | Where Scrybble drops PDFs |
| `OUT_DIR` | `/out` | **Transcripts output base — its own volume mount.** Mirrors the source subpath under it |
| `OUT_SUBDIR` | `remarkable/_transcripts` | Legacy fallback: used only if `OUT_DIR` is unset (writes inside the vault) |
| `OUT_SUFFIX` | `-handwriting_converted` | Filename = `<source stem><suffix>.md`, e.g. `Carol-handwriting_converted.md` |
| `OUT_ALONGSIDE` | `0` | `1` = write the transcript next to its source PDF (needs a **writable** vault; `OUT_DIR` ignored) |
| `STATE_DIR` | `/state` | Manifest + logs — **must be a persistent volume** |
| `MODEL` | `qwen3.5:9b` | Vision-capable, ~67 s/page neat |
| `OLLAMA_HOST` | `http://ollama:11434` | |
| `THREADS` | `14` | cgroup under-detection workaround |
| `NO_THINK` | `1` | **Required** — thinking ON = unusable |
| `DPI` | `150` | Raising alone does nothing (downscaled to `MAX_PX`) |
| `MAX_PX` | `1568` | The real quality/time lever |
| `TIMEOUT` | `1800` | Per-page socket timeout |
| `INTERVAL` | `600` | Poll seconds |
| `HASH_CHECK` | `1` | `1` = sha256 content detection (authoritative); `0` = last-modified (mtime) detection — cheaper, but re-OCRs on touch-only changes |
| `MAX_AGE_HOURS` | `24` | Only consider PDFs modified within this window; `0` = no limit |
| `MAX_RETRIES` | `3` | Stop retrying a broken PDF |
| `MIN_REPROCESS_INTERVAL` | `0` | Min seconds between reprocesses of the **same** path even if it changed; `0` = off |
| `RUN_WINDOW` | _(empty)_ | Optional, e.g. `01:00-07:00` |
| `LOG_LEVEL` | `INFO` | Set `DEBUG` to log each file's gate decision (see below) |

### Where transcripts go (3 modes)

The filename is always `<source stem><OUT_SUFFIX>.md` (default suffix
`-handwriting_converted`), so `Work/Carol.pdf` → `Carol-handwriting_converted.md`
and `Notes/2026-01-01.pdf` → `2026-01-01-handwriting_converted.md`.

| Mode | Set | Result | Vault mount |
|---|---|---|---|
| **Separate base** (default, recommended) | `OUT_DIR=/out` | mirrors source subpath under `/out` | `:ro` |
| **Inside the vault** (Obsidian-indexed) | `OUT_DIR=/vault/_transcripts` (or `OUT_SUBDIR=...`) | mirrors under a vault subfolder | mostly `:ro`, that folder `:rw` |
| **Alongside the source** | `OUT_ALONGSIDE=1` | transcript next to each PDF (`Work/Carol-handwriting_converted.md`) | **`:rw`** |

Alongside mode requires a non-empty `OUT_SUFFIX` (enforced at startup) so a
transcript can never overwrite a source PDF or a Scrybble `.md` stub. The
`/mnt/docker/scrybble/storage` guard stays absolute in every mode.

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
gate=prefilter-skip  Work/Carol.pdf  (mtime+size unchanged, no hash, no OCR)
gate=hash-unchanged  Work/Carol.pdf  (touched but bytes identical, no OCR)
gate=retry-capped    Work/Bad.pdf    (errored 3 times, no OCR)
gate=queued          Work/Carol.pdf  (changed -> will OCR)
```

## Dependencies

The code is three plain Python files (`rm_ocr.py`, `ocr_daemon.py`, `selftest.py`)
and is **pure standard library** except for one rasterizer:

- **`pdf2image`** (pip — see `requirements.txt`; pulls in Pillow) + **poppler**
  (system: `apt-get install poppler-utils` / `brew install poppler`).
- **Ollama** running with the model pulled: `ollama pull qwen3.5:9b`.
- *Optional:* `rmc` (`pipx install rmc`) — only to render raw reMarkable `.zip`
  bundles; the vault's `*.pdf` path never touches it.

The Docker image bakes poppler + `pdf2image` in, so in the container the only
external requirement is a reachable Ollama. `selftest.py` stubs `pdf2image` and
Ollama, so it runs with **no dependencies at all**: `python3 selftest.py`.

## Transcript format

```markdown
---
source: remarkable/Work/Carol.pdf
model: qwen3.5:9b
source_modified: 2026-05-30T09:14:02
processed_at: 2026-05-30T12:00:00
pages: 3
chars_per_page: [812, 640, 91]
status: ok
---

# Carol

Source: [[remarkable/Work/Carol.pdf]]

## Page 1

...transcription...
```

## State

`STATE_DIR/manifest.json` keyed by vault-relative PDF path:
`{ mtime, size, sha256, source_modified, out_path, pages, chars_per_page, processed_at, status, retries }`.
Written atomically (temp file + rename). `STATE_DIR/ocr.log` mirrors stdout.
