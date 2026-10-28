# =============================================
# FluxFetch | Universal Downloader (v3)
# Fixed: unzip for Deno + JS runtime support
# =============================================
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (required by yt-dlp for JS challenges)
# Using the official script; unzip is now available
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# Verify Deno installation (fails fast if something went wrong)
RUN deno --version

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
