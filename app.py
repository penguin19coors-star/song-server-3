from flask import Flask, request, jsonify, send_file
import subprocess
import os
import uuid
import threading
import time

app = Flask(__name__)
AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

# Path to your YouTube cookies file (Netscape format).
# Export with a browser extension like "Get cookies.txt LOCALLY" while logged
# into a THROWAWAY YouTube account (real accounts can get banned).
# Set the YT_COOKIES_FILE env var, or drop the file at the default path below.
COOKIES_FILE = os.environ.get("YT_COOKIES_FILE", "/etc/secrets/cookies.txt")

# Optional proxy (residential proxies work best — cloud IPs are heavily flagged).
# Example: http://user:pass@host:port
PROXY_URL = os.environ.get("PROXY_URL", "")


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

# Player clients to try, in order. If YouTube blocks one, the next is tried.
# Each is a separate yt-dlp invocation so we can detect failure and fall back.
PLAYER_CLIENT_FALLBACKS = [
    "default",
    "web_safari",
    "mweb",
    "tv",
    "ios",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)


def _looks_like_bot_block(stderr: str) -> bool:
    if not stderr:
        return False
    s = stderr.lower()
    needles = [
        "sign in to confirm",
        "confirm you're not a bot",
        "this video is not available",
        "http error 403",
        "http error 429",
        "unable to extract",
        "po token",
        "precondition check failed",
    ]
    return any(n in s for n in needles)


def _run_ytdlp(query, output_template, player_client):
    """Run yt-dlp once with a specific player_client. Returns CompletedProcess."""
    cmd = [
        "yt-dlp",
        "-f", "bestaudio",
        "--no-playlist",
        "-x",
        "--audio-format", "mp3",
        "--no-warnings",
        "--no-check-certificates",
        "--extractor-args", f"youtube:player_client={player_client}",
        "--user-agent", USER_AGENT,
        # Light throttling helps avoid rate-flagging
        "--sleep-requests", "1",
        "--sleep-interval", "2",
        "--max-sleep-interval", "5",
        # Retries within yt-dlp for transient issues
        "--retries", "3",
        "--fragment-retries", "3",
        "-o", output_template,
    ]

    if os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]

    if PROXY_URL:
        cmd += ["--proxy", PROXY_URL]

    cmd.append(f"ytsearch1:{query}")

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=180,
    )


def download_and_convert(query, safe_name, file_id, output_template,
                         quality="high", max_seconds="600"):
    """Step 1: Download audio (with fallback player clients).
       Step 2: Convert with ffmpeg at desired quality."""

    last_err = None
    result = None

    # Try each player_client until one succeeds
    for client in PLAYER_CLIENT_FALLBACKS:
        result = _run_ytdlp(query, output_template, client)

        # Look for a downloaded file
        raw_file = None
        for f in os.listdir(AUDIO_DIR):
            if f.startswith(safe_name) and f.endswith(".mp3"):
                raw_file = os.path.join(AUDIO_DIR, f)
                break

        if raw_file and os.path.exists(raw_file):
            break  # success!

        last_err = result.stderr[-500:] if result and result.stderr else "no error output"

        # If it doesn't look like a bot/auth issue, no point trying other clients
        if not _looks_like_bot_block(result.stderr if result else ""):
            break

        # Brief pause before fallback attempt
        time.sleep(1)

    # Recompute raw_file after the loop
    raw_file = None
    for f in os.listdir(AUDIO_DIR):
        if f.startswith(safe_name) and f.endswith(".mp3"):
            raw_file = os.path.join(AUDIO_DIR, f)
            break

    if not raw_file or not os.path.exists(raw_file):
        return None, last_err or "download failed"

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
        "cookies_loaded": os.path.exists(COOKIES_FILE),
        "proxy_enabled": bool(PROXY_URL),
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
    return jsonify({
        "status": "ok",
        "cookies_loaded": os.path.exists(COOKIES_FILE),
    })


@app.route("/download", methods=["GET"])
def download_audio():
    """Downloads audio from YouTube, converts to high-quality MP3, returns a direct URL"""
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
        filepath, error = download_and_convert(
            query, safe_name, file_id, output_template, quality, max_seconds
        )

        if not filepath:
            return jsonify({
                "error": "Could not find or convert audio",
                "stderr": error,
                "hint": "If you see bot-check errors, make sure cookies.txt is present and yt-dlp is up to date.",
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
        filepath, error = download_and_convert(
            query, safe_name, file_id, output_template, quality, max_seconds
        )

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
