FROM python:3.11-slim

# Install system deps: ffmpeg + Node.js + curl + git
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

# Force latest yt-dlp + yt-dlp-ejs (busts pip layer cache via the ARG below).
# yt-dlp-ejs ships the JavaScript challenge solver scripts that yt-dlp needs
# to decrypt YouTube stream URLs ("signature" and "n" challenges).
ARG YTDLP_CACHE_BUST=2
RUN pip install --no-cache-dir --upgrade --force-reinstall --pre yt-dlp yt-dlp-ejs

# --- bgutil POT provider HTTP server (Node.js) ---
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
