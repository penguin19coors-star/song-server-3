from flask import Flask, request, jsonify, send_file
import subprocess
import os
import uuid
import threading
import time

app = Flask(__name__)
AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)


def cleanup_old_files():
    """Delete audio files older than 60 minutes"""
    while True:
        now = time.time()
        for f in os.listdir(AUDIO_DIR):
            path = os.path.join(AUDIO_DIR, f)
            try:
                if now - os.path.getmtime(path) > 3600:
                    os.remove(path)
            except Exception:
                pass
        time.sleep(60)


threading.Thread(target=cleanup_old_files, daemon=True).start()


def make_safe_name(query):
    """Turn a search query into a safe filename"""
    safe = "".join(c if c.isalnum() or c in " -" else "" for c in query)
    safe = safe[:50].strip().replace(" ", "_")
    return safe


# Quality presets
QUALITY_PRESETS = {
    "low": {"bitrate": "64k", "sample_rate": "22050", "channels": "1"},
    "medium": {"bitrate": "128k", "sample_rate": "44100", "channels": "2"},
    "high": {"bitrate": "192k", "sample_rate": "44100", "channels": "2"},
    "max": {"bitrate": "320k", "sample_rate": "44100", "channels": "2"},
}


def download_and_convert(query, safe_name, file_id, output_template, quality="high", max_seconds="600"):
    """Step 1: Download full audio. Step 2: Convert with ffmpeg at desired quality."""

    # Step 1: Download best available audio
    result = subprocess.run(
        [
            "yt-dlp",
            "-f", "bestaudio",
            "--no-playlist",
            "-x",
            "--audio-format", "mp3",
            "--no-warnings",
            "--no-check-certificates",
            "--extractor-args", "youtube:player_client=mediaconnect",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "-o", output_template,
            f"ytsearch1:{query}",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    # Find the downloaded file
    raw_file = None
    for f in os.listdir(AUDIO_DIR):
        if f.startswith(safe_name) and f.endswith(".mp3"):
            raw_file = os.path.join(AUDIO_DIR, f)
            break

    if not raw_file or not os.path.exists(raw_file):
        return None, result.stderr[-500:] if result.stderr else "no error output"

    # Step 2: Re-encode with ffmpeg at desired quality
    preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["high"])
    compressed_file = raw_file.replace(".mp3", "_hq.mp3")

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", raw_file,
            "-t", max_seconds,
            "-b:a", preset["bitrate"],
            "-ac", preset["channels"],
            "-ar", preset["sample_rate"],
            compressed_file,
        ],
        capture_output=True,
        timeout=60,
    )

    if os.path.exists(compressed_file):
        os.remove(raw_file)
        os.rename(compressed_file, raw_file)
        return raw_file, None
    else:
        return raw_file, None


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "High-quality MP3 server is running!",
        "endpoints": {
            "/download": "Returns JSON with direct URL to MP3",
            "/stream": "Serves MP3 directly",
            "/files/<filename>": "Serves stored MP3 by filename",
        },
        "quality_options": {
            "low": "64kbps mono (smallest, ~480KB per minute)",
            "medium": "128kbps stereo (~960KB per minute)",
            "high": "192kbps stereo (~1.4MB per minute) [default]",
            "max": "320kbps stereo (~2.4MB per minute)",
        },
        "usage": "/download?q=song+name&quality=high&sec=600",
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/download", methods=["GET"])
def download_audio():
    """Downloads audio from YouTube, converts to high-quality MP3, and returns a direct URL"""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "No query provided. Use ?q=song+name"}), 400

    quality = request.args.get("quality", "high")
    if quality not in QUALITY_PRESETS:
        return jsonify({"error": f"Invalid quality. Options: {list(QUALITY_PRESETS.keys())}"}), 400

    max_seconds = request.args.get("sec", "600")

    file_id = str(uuid.uuid4())[:8]
    safe_name = make_safe_name(query)
    filename_base = f"{safe_name}_{file_id}"
    output_template = os.path.join(AUDIO_DIR, f"{filename_base}.%(ext)s")

    try:
        filepath, error = download_and_convert(query, safe_name, file_id, output_template, quality, max_seconds)

        if not filepath:
            return jsonify({
                "error": "Could not find or convert audio",
                "stderr": error,
            }), 500

        filename = os.path.basename(filepath)
        base_url = request.host_url.rstrip("/").replace("http://", "https://")
        file_url = f"{base_url}/files/{filename}"
        file_size = os.path.getsize(filepath)
        file_size_mb = round(file_size / (1024 * 1024), 2)

        return jsonify({
            "url": file_url,
            "filename": filename,
            "size_bytes": file_size,
            "size_mb": file_size_mb,
            "quality": quality,
            "bitrate": QUALITY_PRESETS[quality]["bitrate"],
            "query": query,
            "expires_in": "60 minutes",
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Download timed out"}), 504


@app.route("/stream", methods=["GET"])
def stream_audio():
    """Downloads audio and serves it directly as a high-quality MP3"""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "No query provided. Use ?q=song+name"}), 400

    quality = request.args.get("quality", "high")
    if quality not in QUALITY_PRESETS:
        quality = "high"

    max_seconds = request.args.get("sec", "600")

    file_id = str(uuid.uuid4())[:8]
    safe_name = make_safe_name(query)
    filename_base = f"{safe_name}_{file_id}"
    output_template = os.path.join(AUDIO_DIR, f"{filename_base}.%(ext)s")

    try:
        filepath, error = download_and_convert(query, safe_name, file_id, output_template, quality, max_seconds)

        if not filepath:
            return jsonify({
                "error": "Could not find or convert audio",
                "stderr": error,
            }), 500

        return send_file(filepath, mimetype="audio/mpeg")

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Download timed out"}), 504


@app.route("/files/<filename>", methods=["GET"])
def serve_file(filename):
    """Serves a stored MP3 file directly by filename"""
    if "/" in filename or ".." in filename:
        return jsonify({"error": "Invalid filename"}), 400

    filepath = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found or expired"}), 404

    return send_file(filepath, mimetype="audio/mpeg")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
