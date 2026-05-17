FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Force yt-dlp to the latest version on every build (busts Docker layer cache).
# Bump this number whenever you want to force a fresh yt-dlp install.
ARG YTDLP_CACHE_BUST=1
RUN pip install --no-cache-dir --upgrade --force-reinstall --pre yt-dlp

COPY . .

CMD gunicorn --bind 0.0.0.0:8080 --timeout 180 --workers 2 app:app
