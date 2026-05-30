FROM python:3.12-slim

# poppler-utils -> pdftoppm/pdfunite for pdf2image and bundle merging
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY rm_ocr.py ocr_daemon.py /app/

# Unbuffered so logs stream to `docker logs` in real time.
ENV PYTHONUNBUFFERED=1

# Default: run as the daemon (scan -> process -> sleep INTERVAL, forever).
# For a one-shot cron-style pass, override the command with: --scan
ENTRYPOINT ["python3", "ocr_daemon.py"]
