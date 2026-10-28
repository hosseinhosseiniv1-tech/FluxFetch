# =============================================
# FluxFetch | Universal Downloader (v2)
# Includes: ffmpeg, Deno (JS runtime for yt-dlp), app.py
# =============================================
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Deno — required by yt-dlp for YouTube JS challenges
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app.py ./

RUN mkdir -p /app/downloads \
    && chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNLOAD_DIR=/app/downloads \
    BOT_OWNER_HAS_PREMIUM=false

CMD ["python", "-u", "app.py"]
