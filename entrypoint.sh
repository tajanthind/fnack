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

# Start the bundled PO-token provider (bgutil-ytdlp-pot-provider) on 127.0.0.1:4416.
# yt-dlp uses these long-lived tokens to bypass YouTube bot-checks without cookies.
if [ -f /opt/bgutil-provider/server/build/main.js ] && ! pgrep -f "bgutil-provider/server/build/main.js" > /dev/null 2>&1; then
    echo "[FNACK] Starting bundled PO-token provider on 127.0.0.1:4416..."
    node /opt/bgutil-provider/server/build/main.js --port 4416 > /tmp/bgutil-provider.log 2>&1 &
    sleep 1
fi

# ---------------------------------------------------------------------------
# Optional VPN support (single container).
# Place your config in the mounted /config/vpn/ directory:
#   - /config/vpn/*.ovpn          -> OpenVPN client config (most providers)
#   - /config/vpn/wg0.conf        -> WireGuard config
# The tunnel routes ALL container traffic (YouTube downloads, Deezer metadata,
# etc.) through the VPN. Without any config, the container runs without a VPN.
# Requires the container to run with --cap-add=NET_ADMIN and /dev/net/tun.
# ---------------------------------------------------------------------------
VPN_DIR=/config/vpn
if [ -d "$VPN_DIR" ] && ls "$VPN_DIR"/*.ovpn > /dev/null 2>&1; then
    OVPN=$(ls "$VPN_DIR"/*.ovpn | head -1)
    echo "[FNACK] VPN: starting OpenVPN with $OVPN"
    openvpn --config "$OVPN" --daemon --log /tmp/openvpn.log --cd "$VPN_DIR" \
      || echo "[FNACK] WARNING: OpenVPN failed to start (see /tmp/openvpn.log)"
    # Wait briefly for the tunnel to come up (best effort)
    for i in $(seq 1 15); do
        if ip addr show tun0 > /dev/null 2>&1 || ip route show | grep -q "tun0"; then
            echo "[FNACK] VPN: tunnel tun0 is up"
            break
        fi
        sleep 1
    done
elif [ -f "$VPN_DIR/wg0.conf" ]; then
    echo "[FNACK] VPN: starting WireGuard with $VPN_DIR/wg0.conf"
    if ! WG_OUTPUT=$(wg-quick up "$VPN_DIR/wg0.conf" 2>&1); then
        echo "[FNACK] WARNING: WireGuard failed to start:" >&2
        echo "$WG_OUTPUT" | tail -6 >&2
        echo "[FNACK] Hint: needs kernel wireguard or --cap-add=NET_ADMIN + /dev/net/tun" >&2
    fi
fi

# Execute the primary container command
exec "$@"
