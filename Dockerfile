# Stage 1: bundled PO-token provider (bgutil-ytdlp-pot-provider HTTP server).
# Long-lived YouTube Proof-of-Origin tokens help bypass bot-checks without
# cookies. It runs INSIDE the fnack container (single-container deployment).
FROM brainicism/bgutil-ytdlp-pot-provider:1.3.2 AS bgutil-pot

FROM python:3.11-slim-bookworm

# Node 22: the bundled POT provider requires node >= 20, and a modern JS
# runtime improves yt-dlp challenge solving. openvpn/wireguard-tools/iproute2
# provide optional in-container VPN support (configs in /config/vpn/).
# openresolv provides the `resolvconf` command wg-quick needs for DNS when
# bringing up wg0 — without it tunnels die with "resolvconf: command not
# found" right after the interface comes up. (The Debian `resolvconf` package
# cannot be used: its postinst symlinks /etc/resolv.conf, which is a
# bind-mounted file in Docker and fails with "Device or resource busy".)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg nodejs chromium xvfb x11-utils procps curl unzip xz-utils ca-certificates openvpn wireguard-tools iproute2 openresolv && \
    curl -fsSL https://nodejs.org/dist/v22.17.0/node-v22.17.0-linux-x64.tar.xz -o /tmp/node22.tar.xz && \
    tar -xJf /tmp/node22.tar.xz -C /usr/local --strip-components=1 && \
    rm -f /tmp/node22.tar.xz && \
    curl -fsSL https://deno.land/install.sh | sh && \
    cp /root/.deno/bin/deno /usr/local/bin/deno && \
    rm -rf /var/lib/apt/lists/*

# Bundle the POT provider server (built, with node_modules)
COPY --from=bgutil-pot /app /opt/bgutil-provider/server

ENV CHROME_PATH=/usr/bin/chromium \
    DISPLAY=:99 \
    SPOTIFLAC_REGISTRIES=https://raw.githubusercontent.com/spotiflacapp/SpotiFLAC-Extension/refs/heads/main/registry.json \
    POT_PROVIDER_URL=http://127.0.0.1:4416 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/entrypoint.sh

RUN mkdir -p /config /downloads /music
VOLUME ["/config", "/downloads", "/music"]

EXPOSE 4688

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:4688/api/artists || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
# --timeout 300: Spotify search/SpotiFLAC subprocess runs can exceed gunicorn's default 30s
# silent-worker timeout, which would otherwise kill the worker mid-download.
CMD ["gunicorn", "-k", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", "-w", "1", "-b", "0.0.0.0:4688", "--timeout", "300", "--graceful-timeout", "60", "--access-logfile", "-", "--error-logfile", "-", "app:app"]