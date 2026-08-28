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

# Split-mode VPN helpers: only download/metadata traffic goes through the
# tunnel. wg-quick normally routes the WHOLE container via wg0, which makes
# the web dashboard unreachable (responses to the LAN browser blackhole into
# the tunnel) and, when the peer handshake is incomplete, breaks everything.
# We instead keep the normal default route and send only the HTTP proxy's
# sockets (uid 2001) through wg0; downloads/lookups use the proxy.
VPN_PROXY_PORT="${FNACK_VPN_PROXY_PORT:-1080}"
VPN_PROXY_UID=2001

_setup_split_mode() {
    # Undo wg-quick's whole-container routing; table 51820 still has
    # 'default dev wg0' (the tunnel) — now reachable only by the proxy uid.
    ip rule del not fwmark 51820 table 51820 2>/dev/null || true
    ip rule del table main suppress_prefixlength 0 2>/dev/null || true
    ip rule add uidrange "$VPN_PROXY_UID-$VPN_PROXY_UID" lookup 51820 2>/dev/null || true

    # Local HTTP CONNECT proxy whose outbound sockets egress via wg0.
    if ! pgrep -f "scripts/http_proxy.py" > /dev/null 2>&1; then
        python3 /app/scripts/http_proxy.py --port "$VPN_PROXY_PORT" --uid "$VPN_PROXY_UID" \
            > /tmp/fnack-http-proxy.log 2>&1 &
        sleep 0.5
    fi

    export HTTP_PROXY="http://127.0.0.1:$VPN_PROXY_PORT"
    export HTTPS_PROXY="http://127.0.0.1:$VPN_PROXY_PORT"
    export ALL_PROXY="http://127.0.0.1:$VPN_PROXY_PORT"
    export NO_PROXY="localhost,127.0.0.1,::1,192.168.0.0/16,172.16.0.0/12,100.64.0.0/10"
    echo "[FNACK] VPN: split mode active — downloads/metadata via tunnel (proxy 127.0.0.1:$VPN_PROXY_PORT), dashboard/LAN direct"
}

_teardown_split_mode() {
    ip rule del uidrange "$VPN_PROXY_UID-$VPN_PROXY_UID" lookup 51820 2>/dev/null || true
    pkill -f "scripts/http_proxy.py" 2>/dev/null || true
    unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
}

# ---------------------------------------------------------------------------
# Optional VPN support (single container).
# Place your config in the mounted /config/vpn/ directory:
#   - /config/vpn/*.ovpn          -> OpenVPN client config (most providers)
#   - /config/vpn/wg0.conf        -> WireGuard config
# The tunnel is used in SPLIT MODE: only fnack's downloads and metadata
# lookups route through it (via the bundled HTTP proxy), so the web dashboard
# and your LAN stay fully reachable even while the VPN is up.
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
    echo "[FNACK] VPN: starting WireGuard (split mode) with $VPN_DIR/wg0.conf"
    RESOLV_BACKUP=""
    if [ -f /etc/resolv.conf ]; then
        RESOLV_BACKUP=$(mktemp)
        cp /etc/resolv.conf "$RESOLV_BACKUP"
    fi
    if WG_OUTPUT=$(wg-quick up "$VPN_DIR/wg0.conf" 2>&1); then
        echo "[FNACK] VPN: WireGuard tunnel is up"
        _setup_split_mode
        # Restore Docker's resolver (wg-quick repoints /etc/resolv.conf at the
        # VPN's DNS, which is unreachable until the peer handshake completes).
        if [ -n "$RESOLV_BACKUP" ] && [ -f /etc/resolv.conf ]; then
            cp "$RESOLV_BACKUP" /etc/resolv.conf
            rm -f "$RESOLV_BACKUP"
        fi
    else
        echo "[FNACK] WARNING: WireGuard failed to start:" >&2
        echo "$WG_OUTPUT" | tail -6 >&2
        echo "[FNACK] Hint: the container must run with --cap-add=NET_ADMIN, --device=/dev/net/tun" >&2
        echo "[FNACK]       and --sysctl net.ipv4.conf.all.src_valid_mark=1 (see docker-compose.yml)." >&2
        [ -n "$RESOLV_BACKUP" ] && rm -f "$RESOLV_BACKUP"
    fi
fi

# Execute the primary container command
exec "$@"
