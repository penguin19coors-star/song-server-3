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
print(f"[cookies] Using cookies file: {COOKIES_FILE or '(none)'}")

PROXY_URL = os.environ.get("PROXY_URL", "")
POT_SERVER_URL = os.environ.get("POT_SERVER_URL", "http://127.0.0.1:4416")

# JavaScript runtime for solving YouTube's signature/n challenges.
# Node.js is installed in the Dockerfile for the POT server, so we reuse it.
JS_RUNTIME = os.environ.get("JS_RUNTIME", "node")


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

# `tv` and `ios` clients aren't affected by SABR streaming forcing,
# so they're more reliable than web-based clients for audio extraction.
PLAYER_CLIENT_FALLBACKS = ["tv", "ios", "default", "mweb"]

FORMAT_SELECTOR = "bestaudio/best[acodec!=none]/best"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)

PER_CLIENT_TIMEOUT = 35
MAX_CLIENTS_TO_TRY = 3


def _looks_like_recoverable_error(stderr: str) -> bool:
    if not stderr:
        return False
    s = stderr.lower()
    needles = [
        "sign in to confirm",
        "confirm you're not a bot",
        "confirm you\u2019re not a bot",
        "this video is not available",
        "http error 403",
        "http error 429",
        "requested format is not available",
        "no video formats found",
        "unable to extract",
        "no such format",
        "po token",
        "precondition check failed",
        "only images are available",
        "signature solving failed",
        "n challenge solving failed",
    ]
    return any(n in s for n in needles)


def _build_ytdlp_cmd(query_or_url, output_template, player_client, is_url=False):
    """Build a yt-dlp command with all the bells and whistles."""
    cmd = [
        "yt-dlp",
        "-f", FORMAT_SELECTOR,
        "--no-playlist",
        "-x",
        "--audio-format", "mp3",
        "--no-warnings",
        "--no-check-certificates",
        # JS runtime for signature solving (handled by yt-dlp-ejs scripts)
        "--js-runtimes", JS_RUNTIME,
        # Per-extractor args
        "--extractor-args", f"youtube:player_client={player_client}",
        "--extractor-args", f"youtubepot-bgutilhttp:base_url={POT_SERVER_URL}",
        "--user-agent", USER_AGENT,
        "--retries", "1",
        "--fragment-retries", "1",
        "--socket-timeout", "15",
        "-o", output_template,
    ]

    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]

    if PROXY_URL:
        cmd += ["--proxy", PROXY_URL]

    cmd.append(query_or_url if is_url else f"ytsearch1:{query_or_url}")
    return cmd


def _run_ytdlp(query, output_template, player_client):
    cmd = _build_ytdlp_cmd(query, output_template, player_client)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=PER_CLIENT_TIMEOUT
    )


def download_and_convert(query, safe_name, file_id, output_template,
                         quality="high", max_seconds="600"):
    last_err = None
    tried_clients = []

    for client in PLAYER_CLIENT_FALLBACKS[:MAX_CLIENTS_TO_TRY]:
        tried_clients.append(client)
        try:
            result = _run_ytdlp(query, output_template, client)
        except subprocess.TimeoutExpired:
            last_err = f"yt-dlp timed out with client={client}"
            continue

        raw_file = None
        for f in os.listdir(AUDIO_DIR):
            if f.startswith(safe_name) and f.endswith(".mp3"):
                raw_file = os.path.join(AUDIO_DIR, f)
                break

        if raw_file and os.path.exists(raw_file):
            break

        last_err = result.stderr[-500:] if result and result.stderr else "no error output"

        if not _looks_like_recoverable_error(result.stderr if result else ""):
            break

    raw_file = None
    for f in os.listdir(AUDIO_DIR):
        if f.startswith(safe_name) and f.endswith(".mp3"):
            raw_file = os.path.join(AUDIO_DIR, f)
            break

    if not raw_file or not os.path.exists(raw_file):
        return None, {
            "stderr": last_err or "download failed",
            "tried_clients": tried_clients,
            "cookies_loaded": bool(COOKIES_FILE and os.path.exists(COOKIES_FILE)),
            "proxy_enabled": bool(PROXY_URL),
            "pot_server": POT_SERVER_URL,
            "js_runtime": JS_RUNTIME,
        }

    preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["high"])
    compressed_file = raw_file.replace(".mp3", "_hq.mp3")

    try:
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
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        return raw_file, None

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
        "pot_server": POT_SERVER_URL,
        "js_runtime": JS_RUNTIME,
        "proxy_enabled": bool(PROXY_URL),
        "endpoints": {
            "/download": "Returns JSON with direct URL to MP3",
            "/stream": "Serves MP3 directly",
            "/files/<filename>": "Serves stored MP3 by filename",
            "/debug/cookies": "Verify cookies file is loaded",
            "/debug/version": "Check yt-dlp + POT plugin status",
            "/debug/pot": "Check POT server reachability",
            "/debug/trace": "Verbose trace of a download attempt",
        },
        "usage": "/download?q=song+name&quality=high&sec=600",
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


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
            info["non_comment_lines"] = len([
                l for l in content.splitlines() if l and not l.startswith("#")
            ])
            info["mentions_youtube"] = "youtube.com" in content
            info["looks_like_netscape"] = "Netscape HTTP Cookie File" in content[:200]
            info["size_bytes"] = len(content)
        except Exception as e:
            info["read_error"] = str(e)
    return jsonify(info)


@app.route("/debug/version", methods=["GET"])
def debug_version():
    try:
        ver = subprocess.run(
            ["yt-dlp", "--version"], capture_output=True, text=True, timeout=10
        )
        node_ver = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=5
        )
        plugin_check = subprocess.run(
            ["yt-dlp", "-v", "--simulate", "--skip-download",
             "--extractor-args", f"youtubepot-bgutilhttp:base_url={POT_SERVER_URL}",
             "--js-runtimes", JS_RUNTIME,
             "https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
            capture_output=True, text=True, timeout=30,
        )
        relevant = [
            l for l in plugin_check.stderr.splitlines()
            if any(k in l.lower() for k in ["[pot]", "po token", "[jsc]", "ejs", "js runtime", "signature"])
        ]
        return jsonify({
            "yt_dlp_version": ver.stdout.strip(),
            "node_version": node_ver.stdout.strip(),
            "js_runtime": JS_RUNTIME,
            "plugin_debug": relevant[:30],
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/debug/pot", methods=["GET"])
def debug_pot():
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "5", f"{POT_SERVER_URL}/ping"],
            capture_output=True, text=True, timeout=10,
        )
        return jsonify({
            "pot_server_url": POT_SERVER_URL,
            "http_status": result.stdout.strip(),
            "reachable": result.stdout.strip() in ("200", "404"),
        })
    except Exception as e:
        return jsonify({"error": str(e), "pot_server_url": POT_SERVER_URL})


@app.route("/debug/trace", methods=["GET"])
def debug_trace():
    """Run yt-dlp with verbose logging on a specific video."""
    target = request.args.get("url", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    client = request.args.get("client", "tv")

    cmd = [
        "yt-dlp",
        "-v",
        "--simulate",
        "--skip-download",
        "-f", "bestaudio/best",
        "--js-runtimes", JS_RUNTIME,
        "--extractor-args", f"youtube:player_client={client}",
        "--extractor-args", f"youtubepot-bgutilhttp:base_url={POT_SERVER_URL}",
        "--user-agent", USER_AGENT,
        "--socket-timeout", "20",
    ]
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    if PROXY_URL:
        cmd += ["--proxy", PROXY_URL]
    cmd.append(target)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        all_lines = result.stderr.splitlines()
        relevant = [
            l for l in all_lines
            if any(k in l.lower() for k in [
                "[pot]", "po token", "[jsc]", "ejs", "format", "extracted",
                "error", "warning", "innertube", "player response",
                "skipping client", "disabling", "signature", "n challenge",
            ])
        ]
        return jsonify({
            "target": target,
            "client": client,
            "returncode": result.returncode,
            "relevant_log": relevant[-100:],
            "stderr_tail": all_lines[-20:],
            "stdout_tail": result.stdout.splitlines()[-10:],
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out"})


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
                "hint": "Run /debug/trace to see what yt-dlp is actually doing.",
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
            return jsonify({"error": "Could not find or convert audio", "diagnostic": error}), 500

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
