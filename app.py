from flask import Flask, request, jsonify, send_file
import subprocess
import os
import uuid
import threading
import time
import base64

app = Flask(__name__)
AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Cookie loading (three options, in priority order)
#   1. YT_COOKIES_B64   — base64-encoded Netscape cookies.txt
#   2. YT_COOKIES_FILE  — path to cookies.txt on disk
#   3. Default path     — /etc/secrets/cookies.txt
# ---------------------------------------------------------------------------

COOKIES_FILE_PATH = os.environ.get("YT_COOKIES_FILE", "/etc/secrets/cookies.txt")
COOKIES_B64 = os.environ.get("YT_COOKIES_B64", "")
RUNTIME_COOKIES_PATH = "/tmp/yt_cookies.txt"


def _resolve_cookies_file():
    if COOKIES_B64.strip():
        try:
            data = base64.b64decode(COOKIES_B64)
            with open(RUNTIME_COOKIES_PATH, "wb") as f:
                f.write(data)
            os.chmod(RUNTIME_COOKIES_PATH, 0o600)
            return RUNTIME_COOKIES_PATH
        except Exception as e:
            print(f"[cookies] Failed to decode YT_COOKIES_B64: {e}")

    if os.path.exists(COOKIES_FILE_PATH):
        return COOKIES_FILE_PATH

    return ""


COOKIES_FILE = _resolve_cookies_file()
print(f"[cookies] Using cookies file: {COOKIES_FILE or '(none — bot blocks expected)'}")

PROXY_URL = os.environ.get("PROXY_URL", "")


def cleanup_old_files():
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
    safe = "".join(c if c.isalnum() or c in " -" else "" for c in query)
    safe = safe[:50].strip().replace(" ", "_")
    return safe


QUALITY_PRESETS = {
    "low": {"bitrate": "64k", "sample_rate": "22050", "channels": "1"},
    "medium": {"bitrate": "128k", "sample_rate": "44100", "channels": "2"},
    "high": {"bitrate": "192k", "sample_rate": "44100", "channels": "2"},
    "max": {"bitrate": "320k", "sample_rate": "44100", "channels": "2"},
}

# Player clients to try, in order. Different clients expose different formats.
PLAYER_CLIENT_FALLBACKS = ["default", "web_safari", "mweb", "tv", "ios", "android"]

# Format selector with fallbacks:
#   1. Best audio-only stream
#   2. Best stream that has audio (combined video+audio)
#   3. Anything (last resort — ffmpeg will extract audio later)
FORMAT_SELECTOR = "bestaudio/best[acodec!=none]/best"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)


def _looks_like_recoverable_error(stderr: str) -> bool:
    """Check if the error might be fixed by trying a different player_client."""
    if not stderr:
        return False
    s = stderr.lower()
    needles = [
        # Bot detection
        "sign in to confirm",
        "confirm you're not a bot",
        "confirm you\u2019re not a bot",
        "this video is not available",
        # HTTP errors
        "http error 403",
        "http error 429",
        # Format/extraction issues — different clients expose different formats
        "requested format is not available",
        "no video formats found",
        "unable to extract",
        "no such format",
        # PO token issues
        "po token",
        "precondition check failed",
    ]
    return any(n in s for n in needles)


def _run_ytdlp(query, output_template, player_client):
    cmd = [
        "yt-dlp",
        "-f", FORMAT_SELECTOR,
        "--no-playlist",
        "-x",
        "--audio-format", "mp3",
        "--no-warnings",
        "--no-check-certificates",
        "--extractor-args", f"youtube:player_client={player_client}",
        "--user-agent", USER_AGENT,
        "--sleep-requests", "1",
        "--sleep-interval", "2",
        "--max-sleep-interval", "5",
        "--retries", "3",
        "--fragment-retries", "3",
        "-o", output_template,
    ]

    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]

    if PROXY_URL:
        cmd += ["--proxy", PROXY_URL]

    cmd.append(f"ytsearch1:{query}")

    return subprocess.run(cmd, capture_output=True, text=True, timeout=180)


def download_and_convert(query, safe_name, file_id, output_template,
                         quality="high", max_seconds="600"):
    last_err = None
    tried_clients = []

    for client in PLAYER_CLIENT_FALLBACKS:
        tried_clients.append(client)
        result = _run_ytdlp(query, output_template, client)

        raw_file = None
        for f in os.listdir(AUDIO_DIR):
            if f.startswith(safe_name) and f.endswith(".mp3"):
                raw_file = os.path.join(AUDIO_DIR, f)
                break

        if raw_file and os.path.exists(raw_file):
            break

        last_err = result.stderr[-500:] if result and result.stderr else "no error output"

        # If the error doesn't look recoverable by trying another client, stop
        if not _looks_like_recoverable_error(result.stderr if result else ""):
            break

        time.sleep(1)

    raw_file = None
    for f in os.listdir(AUDIO_DIR):
        if f.startswith(safe_name) and f.endswith(".mp3"):
            raw_file = os.path.join(AUDIO_DIR, f)
            break

    if not raw_file or not os.path.exists(raw_file):
        diagnostic = {
            "stderr": last_err or "download failed",
            "tried_clients": tried_clients,
            "cookies_loaded": bool(COOKIES_FILE and os.path.exists(COOKIES_FILE)),
            "proxy_enabled": bool(PROXY_URL),
        }
        return None, diagnostic

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
        "cookies_loaded": bool(COOKIES_FILE and os.path.exists(COOKIES_FILE)),
        "cookies_source": COOKIES_FILE or "none",
        "proxy_enabled": bool(PROXY_URL),
        "endpoints": {
            "/download": "Returns JSON with direct URL to MP3",
            "/stream": "Serves MP3 directly",
            "/files/<filename>": "Serves stored MP3 by filename",
            "/debug/cookies": "Verify cookies file is loaded and not stale",
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
        "cookies_loaded": bool(COOKIES_FILE and os.path.exists(COOKIES_FILE)),
    })


@app.route("/debug/cookies", methods=["GET"])
def debug_cookies():
    info = {
        "cookies_path": COOKIES_FILE,
        "exists": bool(COOKIES_FILE and os.path.exists(COOKIES_FILE)),
    }
    if info["exists"]:
        try:
            with open(COOKIES_FILE, "r") as f:
                content = f.read()
            lines = [l for l in content.splitlines() if l and not l.startswith("#")]
            info["non_comment_lines"] = len(lines)
            info["mentions_youtube"] = "youtube.com" in content
            info["looks_like_netscape"] = (
                content.startswith("# Netscape HTTP Cookie File")
                or "Netscape HTTP Cookie File" in content[:200]
            )
            info["size_bytes"] = len(content)
        except Exception as e:
            info["read_error"] = str(e)
    return jsonify(info)


@app.route("/download", methods=["GET"])
def download_audio():
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
                "diagnostic": error,
                "hint": "If you still see errors after this, try updating yt-dlp to the latest version.",
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
                "diagnostic": error,
            }), 500

        return send_file(filepath, mimetype="audio/mpeg")

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Download timed out"}), 504


@app.route("/files/<filename>", methods=["GET"])
def serve_file(filename):
    if "/" in filename or ".." in filename:
        return jsonify({"error": "Invalid filename"}), 400

    filepath = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found or expired"}), 404

    return send_file(filepath, mimetype="audio/mpeg")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
