# Security policy

## Reporting a vulnerability

**Please don't open a public issue for a security finding.** Use GitHub's
[private vulnerability reporting][gh-pvr] instead — that opens a confidential
thread that only the maintainers can see:

  https://github.com/delize/remarkable-ocr-handwriting/security/advisories/new

Include enough detail to reproduce. A proof-of-concept, a minimal config that
triggers it, or a CVE reference if you've already filed one. We'll
acknowledge within a few days and work with you on a fix + disclosure timing
before any public write-up.

If you can't use GitHub's flow for some reason, the maintainer's contact info
is on their GitHub profile.

## What's in scope

- The `rm-ocr` Python code (`rm_ocr.py`, `ocr_daemon.py`, `rm_render.py`,
  `rm_split.py`).
- The container image published to GHCR (`ghcr.io/delize/remarkable-ocr-handwriting`).
- The workflow files under `.github/workflows/` — supply-chain issues
  (e.g. an action with excessive permissions) count.

## What's *not* in scope

- CVEs in upstream pinned dependencies (`pdf2image`, `pypdf`, `rmc`,
  `rmscene`, `Pillow`, `numpy`, `inotify_simple`). Dependabot bumps these
  weekly and the CI pipeline gates merges on Trivy + pip-audit; report
  those upstream.
- The base image (`python:3.14-slim` → Debian). Unfixed Debian CVEs are
  flagged in our Trivy reports but fixing them is upstream Debian's call.
- The reMarkable cloud, Ollama, Scrybble, Obsidian, or any other external
  system this tool integrates with — please report to those projects.

## Supported versions

Only `main` and the latest tagged release (`vX.Y.Z`) get security fixes.
Older tags are immutable historical artifacts; we won't re-publish them.

## What we do on our side

- Trivy scans every PR build and gates merges on **CRITICAL** + **HIGH**
  fixable CVEs.
- pip-audit scans `requirements.txt` on every PR; findings block merge.
- gitleaks scans the full git history on every PR for committed secrets.
- CodeQL static analysis runs on every PR and weekly on `main`.
- Dependabot opens PRs weekly for GitHub Actions, Docker base image, and
  pip dependencies.
- Published images carry SBOM + SLSA provenance attestations, viewable via
  `docker buildx imagetools inspect ghcr.io/delize/remarkable-ocr-handwriting:latest`.

[gh-pvr]: https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability
