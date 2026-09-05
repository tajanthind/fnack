# fnack – Production Deployment Guide

fnack is a self-hosted music discography downloader and library
manager. On first boot it asks you to create the initial **admin account**
(whole-app login; passwords stored as salted scrypt hashes — never plaintext). It downloads **true lossless FLAC** via SpotiFLAC (Tidal / Qobuz / Deezer /
SoundCloud) and falls back to **yt-dlp** (YouTube / YouTube Music / SoundCloud) for
everything else. **No Spotify, Tidal, Qobuz, or YouTube account is required.**

This guide covers building and running fnack yourself (no GitHub registry needed).

---

## 1. Quick Start (Docker Compose)

Requirements: Docker Engine 24+ with the Compose plugin, ~3 GB free disk for the image.

```bash
# 1. Get the project (either way works):
#    A) From git:        git clone https://github.com/tajanthind/fnack.git && cd fnack
#    B) No git needed:   see "1b. Deploy Without Git" below, then cd fnack

# 2. Create your .env (music path, secret, concurrency)
cp .env.example .env
nano .env                 # set MUSIC_PATH=/home/you/Music  (defaults to ./music)
                          # SECRET_KEY="$(openssl rand -hex 32)" (optional)
                          # FNACK_COOKIE_SECURE=1  (only behind https)

# 3. Start (pulls the prebuilt image from GitHub Container Registry when
#    possible; use --build to compile locally from this source instead)
docker compose up -d
# or, to always build locally:
# docker compose up -d --build
```

Open the web UI: **http://localhost:4688** (or `http://<server-ip>:4688`).
On the very first boot you are taken to **/setup** to create the initial
admin account — nothing else works until it exists (this also reappears if
the database/config volume is ever reset and no account can be found).
After that, sign in at **/login**.

Machine/API access: send the M2M API key (`X-API-Key: <key>`, shown in
Settings once logged in) instead of a session; the key is optional and keeps
existing integrations (Navidrome-style triggers, Lidarr grabs, scripts)
working under the lockdown.

Check it is healthy:

```bash
docker ps --filter name=fnack          # status should show (healthy)
docker logs -f fnack                    # live logs
```

---

## 1b. Deploy on Another Machine — Without Git

No git is needed on the target machine. Two options:

### Option A — Prebuilt image from GHCR (needs internet, fastest)

GitHub Actions publishes `ghcr.io/tajanthind/fnack:latest` on every push.
Only the `docker-compose.yml` (plus `.env`) is required:

```bash
mkdir fnack && cd fnack
# copy docker-compose.yml and .env.example from any machine / release bundle
cp .env.example .env      # set MUSIC_PATH=...
docker compose up -d      # pulls ghcr.io/tajanthind/fnack:latest
```

### Option B — Release bundle (works offline / fully self-contained)

Build a bundle once on any machine with git:

```bash
./scripts/make_release.sh        # -> fnack-release-<version>.tar.gz (120 KB)
```

Copy `fnack-release-<version>.tar.gz` to the target machine
(scp / USB stick / any transfer), then:

```bash
tar xzf fnack-release-<version>.tar.gz
cd fnack
cp .env.example .env      # set MUSIC_PATH=...
docker compose up -d --build   # builds locally — no git, no registry
```

The bundle contains only git-tracked files, so **cookies, secrets, databases
and `.venv` are never included** (verified by the build script output).

### Option C — Fully offline (no Docker Hub / no apt/pip internet)

On the machine with the image, export it and transfer the file:

```bash
docker save fnack:latest | gzip > fnack-image-<version>.tar.gz   # ~1–2 GB
```

On the target:

```bash
docker load < fnack-image-<version>.tar.gz
# then Option B's compose (or `image: fnack:latest` in docker-compose.yml)
docker compose up -d
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

These steps are provided by fnack's official plugins, each implementing a
capability the core queue resolves (disabling a plugin removes its
capability; core has no provider implementation):

1. **Discography sync** – the `fnack.deezer-batch` plugin (artist.search /
   artist.discography / artist.info / track.metadata) reads metadata from the
   public Deezer API (artist search, albums, tracks, ISRC codes). No account
   needed.
2. **Spotify link resolution** – the `fnack.spotify` plugin (track.resolve)
   does ISRC / title search via DuckDuckGo + Yandex (`ddgs`), plus an
   optional official Spotify API path *only if* you set the plugin's
   `client_id` / `client_secret` (free API credentials, not user accounts).
   Without them, zero-auth search is used.
3. **SpotiFLAC (primary)** – the `fnack.spotiflac` plugin (download.track)
   downloads true lossless FLAC from Tidal / Qobuz / Deezer / SoundCloud. No
   login. Rate-limited + retried automatically. Each track is downloaded
   individually, with per-song yt-dlp fallback via `fnack.ytdlp`.
4. **yt-dlp (fallback)** – the `fnack.ytdlp` plugin (download.track) extracts
   audio from YouTube / YouTube Music / SoundCloud. No login. If YouTube
   enforces a bot-check, fnack automatically retries with the Android player
   client before giving up.

Every downloaded file is **verified before it is accepted into the library**:
  1. **Artist-aware source resolution** — every resolved Spotify URL is checked
     against the expected artist + title (via Spotify's oEmbed endpoint and the
     track page, multi-artist aware) before SpotiFLAC uses it; confirmed
     wrong-artist tracks are rejected and the next candidate is tried.
  2. **Per-candidate yt-dlp verification** — each yt-dlp candidate is checked
     (embedded artist/title tags + variant words like cover/live/remix, plus
     duration when enabled) and wrong candidates are skipped for the next one.
  3. **Final post-download verification** — duration against the official
     release (configurable strictness, toggleable) and embedded tags
     (artist + title) must match; mismatched or corrupted downloads are
     rejected and deleted automatically.

Manual matches (custom URLs) are also tag-verified: if the URL points to a
different song, the download is rejected with a clear message.

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
# (the --sysctl flag is required for WireGuard policy routing; Docker refuses
#  in-container writes to that key, so it must be applied at container start)
docker run -d \
  --name fnack \
  --restart unless-stopped \
  -p 4688:4688 \
  -v "$PWD/config:/config" \
  -v "$PWD/downloads:/downloads" \
  -v "$HOME/Music:/music" \
  --cap-add=NET_ADMIN \
  --device=/dev/net/tun:/dev/net/tun \
  --sysctl net.ipv4.conf.all.src_valid_mark=1 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e MAX_CONCURRENT_DOWNLOADS=1 \
  fnack:latest
```

---

## 6. Upgrading

```bash
# With git:
cd fnack
git pull                       # or copy the new source over
docker compose up -d --build
```

```bash
# Without git (prebuilt image from GHCR):
cd fnack
docker compose pull            # fetch the latest ghcr.io/tajanthind/fnack:latest
docker compose up -d
```

```bash
# Without git (release bundle):
# copy the new fnack-release-<version>.tar.gz over the old source, then:
tar xzf fnack-release-<version>.tar.gz
cd fnack
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
| Downloads fail with "Duration mismatch" | The official track duration doesn't match any search result (common for remixes, live versions, or tracks absent from YouTube/SoundCloud). Options: loosen strictness to Lenient, or disable the **Duration Check** toggle in Settings to accept any downloaded audio |
| Tracks show "missing" but the files exist | Older databases (pre v0.2.04) can have DB/FS drift caused by a fixed watcher race. Run the one-time repair below |
| Downloads not starting | Settings → check `enable_spotiflac` / `enable_ytdlp`; `docker logs fnack` for queue activity |
| Xvfb / display errors | Ensure `DISPLAY=:99` and the container can start Xvfb (auto-handled by entrypoint) |

### Automatic library metadata normalization (Navidrome grouping)

fnack keeps album tags aligned with the database so Navidrome never splits one
album into several entries with the same name. It runs **automatically**:
- at every container start (retroactive pass over the whole library), and
- periodically while running (catches files changed by imports/older versions).

The whole maintenance bundle — duplicate-album merging, tag normalization,
missing-artwork backfill and Navidrome split repair — is governed by **one
setting**: Settings → Performance & Automation → **Library Maintenance
Schedule** (`weekly` = every restart + once a week, the default; `daily`;
`restart`; or `manual`). It always runs in a detached background process, so
the dashboard stays fast regardless of library size. Progress is visible via
`docker logs fnack` (`/tmp/fnack-maintenance.log`); the **Run Maintenance
Now** button (or `POST /api/maintenance/run`) triggers it immediately.

Files that already carry the correct album/albumartist tags are skipped, so
steady-state runs are fast. You can also trigger it manually:

```bash
docker exec fnack python3 /app/scripts/run_maintenance.py
```

Then trigger a Navidrome scan (Settings → Navidrome → Scan) to re-group albums.

### Automatic Navidrome album-split repair (recommended)

If Navidrome already split some albums into multiple rows (e.g. from per-track
release dates left by older fnack versions), fnack can merge those rows
automatically at **every restart** and every 6h. It also takes a snapshot
backup (`navidrome.db.bak-<timestamp>`) before any change.

1. Make the Navidrome database visible to the fnack container by adding a bind
   mount to `docker-compose.yml`:

   ```yaml
   volumes:
     - ./config:/config
     - ./downloads:/downloads
     - ${MUSIC_PATH:-./music}:/music
     - /opt/navidrome/data:/navidrome-data:ro   # <- Navidrome's data dir
   ```

   (Use `:ro` if Navidrome is running on the same host — fnack only needs to
   read; the repair writes to the DB via SQLite WAL. If Navidrome runs on a
   different machine, run the script there instead, or copy the DB.)

2. In the web UI: **Settings → Navidrome → Navidrome Database Path**, enter
   `/navidrome-data/navidrome.db`, and save.

From then on fnack merges any split album rows at every boot and every 6h, and
triggers a Navidrome rescan when it changes something. The **Fix Album Splits
Now** button runs it immediately.

### Optional VPN support (single container)

YouTube bot-checks are far less aggressive from residential IPs. fnack can route
**all of its traffic through a VPN inside the same container** — no extra
container needed. Place your config in the mounted `./config/vpn/` directory:

- **OpenVPN** (most providers): `./config/vpn/<name>.ovpn`
- **WireGuard**: `./config/vpn/wg0.conf`

The compose file already grants the required privileges (`NET_ADMIN` +
`/dev/net/tun`). With no config in `./config/vpn/`, the container runs without a
VPN exactly as before. Example for a manual `docker run`:

```bash
docker run -d --name fnack --cap-add=NET_ADMIN --device=/dev/net/tun:/dev/net/tun ... fnack:latest
```

Notes:
- The image bundles `openresolv` (DNS for wg-quick), `nftables` (tunnel
  firewall rules) and a `sysctl` shim, plus the compose file sets
  `net.ipv4.conf.all.src_valid_mark=1` — all three are required for
  WireGuard to come up inside Docker (v0.2.23+).
- When the tunnel comes up, fnack automatically clears the SpotiFLAC
  rate-limit circuit breaker — so FLAC downloads resume immediately on the
  fresh VPN IP instead of waiting out a 30–300 s cool-down.

### One-time library re-verification (existing downloads)

fnack now guarantees new downloads match their tracks. To audit files that were
downloaded **before** these checks existed, run the re-verification tool (it
fetches the official duration from Deezer, marks confirmed mismatches as failed
— without deleting files — and repairs the stored expected durations):

```bash
docker exec fnack python3 /app/scripts/reverify_library.py
```

Afterwards, mismatched tracks show as failed in the UI; delete and re-download
them (they will now be verified before being accepted).

### One-time library repair (DB/FS drift)

If tracks are marked *missing* while their audio files are still on disk (a
fixed bug in v0.2.04 previously flipped them when files were replaced), run:

```bash
docker exec fnack python3 - <<'EOF'
import os, re, unicodedata
from models import Track, Album, db
from app import app
def norm(s):
    return re.sub(r'[^a-zA-Z0-9]+', '', unicodedata.normalize('NFKD', str(s or ''))).lower()
AUDIO = {'.flac','.mp3','.m4a','.opus','.ogg','.wav','.aac'}
with app.app_context():
    repaired = 0
    for t in Track.query.filter(Track.is_downloaded == False).all():
        album = t.album
        if not album or not album.local_path or not os.path.isdir(album.local_path):
            continue
        d = album.local_path
        nt, tn = norm(t.title), t.track_number
        for f in sorted(os.listdir(d)):
            if not f.lower().endswith(tuple(AUDIO)) or '.tmp' in f:
                continue
            if (tn and f.startswith(f'{tn:02d}. ')) or (nt and nt in norm(os.path.splitext(f)[0])):
                fp = os.path.join(d, f)
                t.is_downloaded = True; t.status = 'completed'; t.progress = 100.0
                t.local_path = fp; t.file_path = os.path.relpath(fp, '/music')
                t.file_format = os.path.splitext(f)[1].lstrip('.')
                t.size_bytes = os.path.getsize(fp); t.error_message = None
                repaired += 1
                break
    for a in Album.query.all():
        trs = a.tracks.all()
        dl = sum(1 for x in trs if x.is_downloaded)
        a.is_downloaded = dl == len(trs) and len(trs) > 0
        a.size_bytes = sum(x.size_bytes or 0 for x in trs)
    db.session.commit()
    print(f'Repaired {repaired} tracks')
EOF
```

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
