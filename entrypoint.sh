#!/bin/bash
set -e

# Ensure required directories exist
mkdir -p /config /downloads /music

# Clean up stale Xvfb artifacts left over from a previous container run.
# Without this, "Fatal server error: Server is already active for display 99"
# prevents the virtual framebuffer from ever starting after a restart.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# Start Xvfb virtual framebuffer on :99 for headless browser resolution
if ! pgrep -f "Xvfb :99" > /dev/null 2>&1; then
    echo "[FNACK] Starting Xvfb virtual framebuffer on display :99..."
    Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
    sleep 0.5
fi

# Verify the display actually came up (fail fast with a clear message instead of silent breakage)
if ! xdpyinfo -display :99 > /dev/null 2>&1; then
    echo "[FNACK] WARNING: Xvfb on display :99 did not become ready; SpotiFLAC browser solving may fail." >&2
fi

# Execute the primary container command
exec "$@"
