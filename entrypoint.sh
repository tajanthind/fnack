#!/bin/bash
set -e

# Ensure required directories exist
mkdir -p /config /downloads /music

# Start Xvfb virtual framebuffer on :99 for headless browser resolution
if ! pgrep -f "Xvfb :99" > /dev/null 2>&1; then
    echo "[FNACK] Starting Xvfb virtual framebuffer on display :99..."
    Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
    sleep 0.5
fi

# Execute the primary container command
exec "$@"
