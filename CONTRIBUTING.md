# Contributing

This is a homelab tool, but contributions are welcome — bug reports, fixes,
ideas, docs improvements. A few light expectations to keep things smooth.

## Filing an issue

Use [GitHub Issues](https://github.com/delize/remarkable-ocr-handwriting/issues).
Helpful to include:

- What you ran (CLI command or compose snippet) and what happened.
- Relevant log lines (with `LOG_LEVEL=DEBUG` if it's gate-related).
- The Ollama version, model tag, and host arch.
- For OCR-quality issues: a redacted sample page if you can share one — note
  that **handwriting samples are personal**; please don't paste real journal
  content. A scribbled page of lorem ipsum proves the same point.

For security issues, **don't open a public issue** — see
[SECURITY.md](SECURITY.md) for private reporting.

## Running the tests

`selftest.py` is the whole test suite. It stubs Ollama, poppler, and the
renderer, so it runs with zero dependencies:

```bash
python3 selftest.py
```

CI runs this on every PR against Python 3.12, 3.13, and 3.14. If you're
touching daemon logic, add a test case for the new behavior — the file is
linear and the fixture pattern is easy to follow.

## Code style

- The codebase is **plain Python**, mostly stdlib, flat module layout at the
  repo root. Don't add a package structure or settings library.
- Ollama is reached via raw `urllib.request` (see `rm_ocr.ocr_pdf` and
  `ocr_daemon.wait_for_model`). Please don't add the `ollama` PyPI package.
- All configuration is env vars at the top of `ocr_daemon.py`. Defaults
  match the working reference deployment; new knobs should follow the same
  pattern (env read + `_env_bool` for booleans).
- No type hints beyond dataclass annotations. The existing code is sparse
  on comments — when you do add one, explain *why*, not *what*.
- Conventional Commit prefixes for commit messages (`feat:`, `fix:`,
  `chore:`, `docs:`, `ci:`, etc.). One logical change per commit.

## Local checks before pushing

```bash
ruff check .                       # Python lint (CI runs this too)
hadolint Dockerfile                # Dockerfile lint (CI runs this too)
python3 selftest.py                # offline test suite
docker build -t rm-ocr:dev .       # confirms the image still builds
```

The CI pipeline (`.github/workflows/ci.yml`) runs all of these plus
gitleaks, pip-audit, actionlint, Trivy, and CodeQL. A clean local run is
usually a clean CI run.

## Architecture context

Worth skimming before a non-trivial change:

- [README — How it works](README.md#how-it-works) — the daemon's poll loop.
- [README — Safety guarantees](README.md#safety-guarantees-enforced-in-code) —
  what the code refuses to do, and why.
- [README — State](README.md#state) — manifest schema and the rendered cache.

The split into `rm_ocr.py` (OCR core), `ocr_daemon.py` (automation),
`rm_render.py` (input dispatch + cache), and `rm_split.py` (tall-page
splitter) is intentional. The CLI and daemon both funnel non-PDF inputs
through `rm_render`; if you're adding a new input type, that's the place.

## Pull requests

- Branch from `main`. Name it descriptively (`feat/some-thing`,
  `fix/the-bug`).
- One PR per change. Easier to review, easier to revert.
- Re-run `selftest.py` after rebasing.
- CI must pass. If it fails on something that looks unrelated to your
  change, ping in the PR — the upstream actions occasionally have
  deprecation churn.
- Update `README.md` / `.env.example` if you added or changed a knob.

Thanks for taking the time.
