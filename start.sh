#!/bin/bash
set -e

# Start the bgutil POT provider HTTP server in the background.
# It listens on 127.0.0.1:4416 by default.
echo "[start] Launching bgutil POT server..."
node /opt/bgutil/server/build/main.js &
BGUTIL_PID=$!

# Give it a moment to bind the port
sleep 3

# Sanity check: is the POT server actually up?
if ! kill -0 $BGUTIL_PID 2>/dev/null; then
    echo "[start] ERROR: bgutil POT server failed to start"
    exit 1
fi
echo "[start] bgutil POT server running (pid $BGUTIL_PID)"

# Trap signals to shut down both processes cleanly
trap "echo '[start] Shutting down...'; kill $BGUTIL_PID 2>/dev/null; exit 0" SIGTERM SIGINT

# Start the Flask app in the foreground (Railway expects this)
echo "[start] Launching gunicorn..."
exec gunicorn --bind 0.0.0.0:8080 --timeout 180 --workers 2 app:app
