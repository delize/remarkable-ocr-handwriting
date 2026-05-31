FROM python:3.14-slim

# OCI labels — link the GHCR package back to the source repo.
LABEL org.opencontainers.image.source="https://github.com/delize/remarkable-ocr-handwriting" \
      org.opencontainers.image.description="reMarkable -> Obsidian handwriting OCR poller (Qwen3-VL via Ollama)"

# poppler-utils -> pdftoppm/pdfunite for pdf2image and bundle merging
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py /app/

# Unbuffered so logs stream to `docker logs` in real time.
ENV PYTHONUNBUFFERED=1

# Default: run as the daemon (scan -> process -> sleep INTERVAL, forever).
# For a one-shot cron-style pass, override the command with: --scan
ENTRYPOINT ["python3", "ocr_daemon.py"]
