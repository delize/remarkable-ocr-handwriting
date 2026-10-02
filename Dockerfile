FROM python:3.14-slim

# OCI labels — link the GHCR package back to the source repo.
LABEL org.opencontainers.image.source="https://github.com/delize/remarkable-ocr-handwriting" \
      org.opencontainers.image.description="reMarkable -> Obsidian handwriting OCR poller (Qwen3-VL via Ollama)"

# poppler-utils -> pdftoppm/pdfunite for pdf2image and bundle merging.
# inkscape -> rmc shells out to it to rasterize its intermediate SVG when
# rendering .zip/.rmdoc/.rm inputs to PDF (not needed for plain .pdf input).
# apt-get upgrade picks up Debian security fixes newer than the base image
# (libpcre2 tripped the CVE gate before python:3.14-slim was rebuilt).
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends poppler-utils inkscape \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
# pip is only needed to build the image. It vendors its own urllib3, msgpack
# and setuptools, which carry fixed HIGH CVEs the scan gates on, and nothing
# at runtime imports pip, so it is removed in the same layer.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip
COPY *.py /app/

# Unbuffered so logs stream to `docker logs` in real time.
ENV PYTHONUNBUFFERED=1

# Default: run as the daemon (scan -> process -> sleep INTERVAL, forever).
# For a one-shot cron-style pass, override the command with: --scan
ENTRYPOINT ["python3", "ocr_daemon.py"]
