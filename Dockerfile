FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cache-busting: change this value (or use --build-arg) to force yt-dlp re-fetch
ARG YTDLP_CACHE_BUST=1
RUN pip install --no-cache-dir --upgrade --force-reinstall --pre yt-dlp

COPY . .
CMD gunicorn --bind 0.0.0.0:8080 --timeout 180 --workers 2 app:app
