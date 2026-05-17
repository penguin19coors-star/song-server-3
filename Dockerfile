FROM python:3.11-slim

# Install system deps: ffmpeg + Node.js (for the bgutil POT server) + curl + git
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        git \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python deps ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Force latest yt-dlp (busts pip layer cache via the ARG below)
ARG YTDLP_CACHE_BUST=1
RUN pip install --no-cache-dir --upgrade --force-reinstall --pre yt-dlp

# --- bgutil POT provider HTTP server (Node.js) ---
# Pin to a known-good version; bump as needed.
ENV BGUTIL_VERSION=1.3.1
RUN git clone --single-branch --branch ${BGUTIL_VERSION} \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc

# --- yt-dlp plugin that talks to the bgutil server ---
RUN pip install --no-cache-dir --upgrade bgutil-ytdlp-pot-provider

# --- App code ---
COPY . .
RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]
