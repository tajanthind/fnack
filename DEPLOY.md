# fnack – Production Deployment Guide

fnack is a self-hosted, zero-authentication music discography downloader and library
manager. It downloads **true lossless FLAC** via SpotiFLAC (Tidal / Qobuz / Deezer /
SoundCloud) and falls back to **yt-dlp** (YouTube / YouTube Music / SoundCloud) for
everything else. **No Spotify, Tidal, Qobuz, or YouTube account is required.**

This guide covers building and running fnack yourself (no GitHub registry needed).

---

## 1. Quick Start (Docker Compose)

Requirements: Docker Engine 24+ with the Compose plugin, ~3 GB free disk for the image.

```bash
# 1. Clone / copy the project and enter its directory
cd fnack

# 2. Choose where your music library lives (default: ~/Music)
export MUSIC_PATH="$HOME/Music"

# 3. (Recommended) Set a fixed secret key so sessions survive restarts
export SECRET_KEY="$(openssl rand -hex 32)"

# 4. Build and start
docker compose up -d --build
```

Open the web UI: **http://localhost:4688** (or `http://<server-ip>:4688`).

Check it is healthy:

```bash
docker ps --filter name=fnack          # status should show (healthy)
docker logs -f fnack                    # live logs
```

---

## 2. What Gets Created

| Path (host)          | Mounted at (container) | Purpose                                  |
| :------------------- | :--------------------- | :--------------------------------------- |
| `./config`           | `/config`              | SQLite database, cookies.txt, settings   |
| `./downloads`        | `/downloads`           | Temporary work directory for downloads   |
| `${MUSIC_PATH}`      | `/music`               | Your organized music library             |

The database, settings, and downloaded music are all persistent across restarts.

---

## 3. How Downloads Work (Zero Auth)

1. **Discography sync** – fnack reads metadata from the public Deezer API
   (artist search, albums, tracks, ISRC codes). No account needed.
2. **Spotify link resolution** – ISRC / title search via DuckDuckGo + Yandex
   (`ddgs`), plus an optional official Spotify API path *only if* you add
   `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` (free API credentials, not
   user accounts). Without them, zero-auth search is used.
3. **SpotiFLAC (primary)** – downloads true lossless FLAC from Tidal / Qobuz /
   Deezer / SoundCloud. No login. Rate-limited + retried automatically.
4. **yt-dlp (fallback)** – extracts audio from YouTube / YouTube Music /
   SoundCloud. No login. If YouTube enforces a bot-check, fnack automatically
   retries with the Android player client before giving up.

Every downloaded file is verified (duration match against the official
release, configurable strictness) and tagged (title, artist, album artist,
album, track/disc number, year, embedded cover art). Mismatched or corrupted
downloads are rejected and deleted automatically.

### Optional: YouTube cookies.txt

If YouTube rate-limits your IP (common on datacenter IPs), provide cookies to
raise limits. This is **optional** — everything works without it.

- **Dashboard upload:** Settings → YouTube Cookies → upload `cookies.txt`
  (export from a signed-in browser with "Get cookies.txt LOCALLY").
- **File mount:** place `cookies.txt` in your host `./config` directory.

---

## 4. Configuration

All settings are editable from the web UI (Settings page) and stored in the
database. Environment variables set defaults at first boot.

| Env var                   | Default        | Description                                    |
| :------------------------ | :------------- | :--------------------------------------------- |
| `SECRET_KEY`              | random         | Flask session key; set a fixed value in prod   |
| `MAX_CONCURRENT_DOWNLOADS`| `3`            | Parallel download workers (1–10)               |
| `CHROME_PATH`             | `/usr/bin/chromium` | Headless browser used by SpotiFLAC         |
| `DISPLAY`                 | `:99`          | Xvfb virtual display for the headless browser  |
| `CONFIG_DIR`              | `/config`      | Where the SQLite DB and cookies live           |
| `SQLALCHEMY_DATABASE_URI` | sqlite in /config | Override the database location (e.g. Postgres) |

### Firewall / ports

Only port **4688** needs to be reachable. The container runs as root by
design (needs to spawn Chromium/Xvfb/ffmpeg); do not expose it to the public
internet without a reverse proxy and, ideally, an auth layer (Authelia /
Authentik / Tailscale).

---

## 5. Building & Running Manually (without compose)

```bash
# Build the image
docker build -t fnack:latest .

# Run it with host bind mounts
docker run -d \
  --name fnack \
  --restart unless-stopped \
  -p 4688:4688 \
  -v "$PWD/config:/config" \
  -v "$PWD/downloads:/downloads" \
  -v "$HOME/Music:/music" \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e MAX_CONCURRENT_DOWNLOADS=3 \
  fnack:latest
```

---

## 6. Upgrading

```bash
# Pull the new code, rebuild, and recreate the container
cd fnack
git pull                       # or copy the new source over
docker compose up -d --build
```

Your database, settings, cookies, and music are untouched (they live in host
directories). On startup, any downloads that were interrupted by the upgrade
are automatically re-queued.

To upgrade a manually-run container:

```bash
docker stop fnack && docker rm fnack
docker build -t fnack:latest .
docker run -d ...   # same command as section 5
```

---

## 7. Backup & Restore

Back up the `config` directory (database + cookies) and your music library.
Nothing else is stateful.

```bash
# Backup (database + cookies + settings)
tar czf fnack-config-backup-$(date +%F).tar.gz config/

# Restore on a new machine
mkdir -p config && tar xzf fnack-config-backup-*.tar.gz
docker compose up -d --build
```

---

## 8. Troubleshooting

| Symptom | Fix |
| :------ | :-- |
| Container shows `(unhealthy)` | `docker logs fnack`; ensure port 4688 is free, check the healthcheck URL |
| SpotiFLAC fails on one track | Expected: some tracks are region-locked or unavailable. fnack falls back to yt-dlp, then marks the track failed — retry later |
| "Sign in to confirm you're not a bot" | Datacenter IP YouTube block. Upload `cookies.txt` (Settings → YouTube Cookies) or wait; the Android-client auto-retry already helps |
| Wrong song downloaded | Raise matching strictness in Settings (Strict ±4s) or use Manual Match with the exact URL |
| Downloads not starting | Settings → check `enable_spotiflac` / `enable_ytdlp`; `docker logs fnack` for queue activity |
| Xvfb / display errors | Ensure `DISPLAY=:99` and the container can start Xvfb (auto-handled by entrypoint) |

---

## 9. Verifying It All Works

```bash
# Health
curl -s http://localhost:4688/health

# Version
curl -s http://localhost:4688/api/version

# Live log stream (watch downloads complete)
docker logs -f fnack

# Check the library directory for organized output
find "$MUSIC_PATH" -type f | head -20
```
