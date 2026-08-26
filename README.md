# fnack

Automated Lossless Music Discography Downloader, Tagger, and Library Manager.

[![Docker](https://img.shields.io/badge/docker-ready-blue.svg?logo=docker)](https://github.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg?logo=python)](https://python.org)

---

## Overview

**fnack** is a lightweight, self-hosted service designed to manage, download, tag, and organize artist discographies. It functions as an automated pipeline for music servers such as **Navidrome**, **Plex**, **Jellyfin**, and **Subsonic**.

---

## Features

### Dual Download Pipeline & Zero-Auth Architecture
- **SpotiFLAC (Primary Lossless)**: Fetches true 16-bit and 24-bit FLAC streams from Tidal, Qobuz, SoundCloud, and Deezer without requiring account authentication.
- **Thread-Safe Rate Limiter & Concurrency Lock**: Features configurable inter-request pacing delay (0.5s–5.0s, default 1.5s) and automatic exponential backoff to eliminate upstream 429 throttling when adding large multi-artist batches.
- **yt-dlp (Intelligent Fallback)**: Direct YouTube & YouTube Music audio extraction with candidate scoring & Topic channel prioritization.
- **YouTube Music Preference**: Prioritizes official record label Topic master audio tracks, automatically avoiding music videos with intrusive intros, dialogue, and sketches.
- **yt-dlp `cookies.txt` Support**: Seamlessly authenticate with YouTube to bypass bot-checks or sign-in blocks. Upload `cookies.txt` via the Dashboard Settings or mount it directly at `/config/cookies.txt`.

### Home Catalogue Metrics & Global Retry
- **Global Overview Stats**: Track Total Artists Managed, Downloaded Tracks, Total Failed Songs, and Total Catalogue Size (dynamically formatted in MB, GB, or TB).
- **One-Click Global Retry**: Prominent "Retry All Failed Songs" button re-queues every failed or error track across your entire library in one click.

### Fast Interactive Library Import (`/import`)
- **Parallel Folder Scan**: Scans your `/music` root concurrently (bounded greenlet pool) with cached Deezer lookups, so even a 50-artist library scans in a few seconds instead of one network round-trip per folder.
- **Multi-Select & Bulk Import**: Check any number of discovered artist folders (or select all) and import them in one batch — each artist is fetched and mapped in the background with a live progress bar, so the web UI stays responsive no matter how many artists you import at once.

### Manual Match & Track Fixer
- **Custom URL Matching**: Click the search icon next to any missing, failed, or downloaded song to fetch from a specific Spotify, YouTube, YouTube Music, or Deezer link.
- **Automatic Retagging**: Downloads the targeted stream, applies uniform tags (Title, Artist, Album, Year, Track/Disc numbers, embedded artwork), and replaces any incorrect file in your library.

### Tagging & Storage Organization
- **Multi-Disc Numbering**: Formats filenames as `{disc}-{track}. {title}.<ext>` to prevent multi-disc track collisions.
- **Tag Normalization**: Strips conflicting casing tags (`ALBUMARTIST`, `AlbumArtist`, `albumartist`) for consistent grouping in Navidrome.
- **Cover Artwork**: Automatically saves `cover.jpg` / `folder.jpg` in album folders and embeds artwork into audio files (FLAC Picture blocks, ID3 APIC, MP4 covr).
- **Subsonic API Integration**: Automatically triggers `startScan.view` on your Navidrome server when downloads finish.

### Song Identification & Strictness
- **ISRC Matching**: Prioritizes exact International Standard Recording Code matching.
- **Duration Profiles**: Strict (±4s), Standard (±8s), and Lenient (±15s) duration matching to filter out live versions, karaoke, or video sketches.
- **Track/Album Monitoring**: Toggle monitoring on individual tracks or complete albums.

---

## YouTube `cookies.txt` Setup Guide

If YouTube restricts downloads or requires sign-in, you can provide a `cookies.txt` file to fnack.

### Step-by-Step Instructions:
1. **Install Cookie Exporter Extension**:
   - Install **Get cookies.txt LOCALLY** from the [Chrome Web Store](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) or [Firefox Add-ons](https://addons.mozilla.org/en-US/firefox/addon/get-cookies-txt-locally/).
2. **Log In to YouTube**:
   - Open [youtube.com](https://www.youtube.com) (or [music.youtube.com](https://music.youtube.com)) in your browser and ensure you are signed in.
3. **Export Cookies**:
   - Click the extension icon on the YouTube tab.
   - Click **Export** or **Export As cookies.txt** and save the file to your computer.
4. **Apply in fnack**:
   - **Method A (Dashboard Upload)**: Go to **Settings** > **YouTube Cookies (cookies.txt)**, select your exported file, and click **Upload File** (or paste the content directly).
   - **Method B (Docker Mount)**: Place the exported file in your host config directory (e.g. `./config/cookies.txt`), which is mounted to `/config/cookies.txt` inside the container.

---

## VPN Setup (Optional, Recommended for YouTube)

If YouTube keeps blocking downloads with *"Sign in to confirm you're not a bot"*, the most effective
fix is to route fnack through a **VPN**. Residential VPN IPs are flagged far less often than
datacenter IPs. fnack runs the VPN **inside its own container** — no extra container needed.

### Option A: Upload via the Web UI (easiest)

1. Get an OpenVPN (`.ovpn`) or WireGuard (`.conf`) config from any VPN provider
   (Mullvad, NordVPN, ProtonVPN, Windscribe, commercial seedboxes, etc.).
2. Open **Settings → VPN (Optional)**.
3. Click **Upload & Apply** and pick your config file (or paste its content).
4. fnack saves it, starts the tunnel, and shows your **public IP** — if it differs
   from your normal IP, the tunnel is working.

You can Start / Stop the VPN and Delete the config any time from the same screen.

### Option B: Drop a file in `./config/vpn/` and restart

```bash
mkdir -p ./config/vpn
cp ~/Downloads/myvpn.ovpn ./config/vpn/   # OpenVPN
# or
cp ~/Downloads/wg0.conf ./config/vpn/     # WireGuard
docker compose up -d                       # restarts with the tunnel
```

### Requirements

The container needs `NET_ADMIN` and `/dev/net/tun` — both are already set in the provided
`docker-compose.yml`:

```yaml
cap_add:
  - NET_ADMIN
devices:
  - /dev/net/tun:/dev/net/tun
```

For a manual `docker run`:

```bash
docker run -d --name fnack --cap-add=NET_ADMIN --device=/dev/net/tun:/dev/net/tun \
  -v ./config:/config -v ./downloads:/downloads -v /path/to/music:/music -p 4688:4688 fnack:latest
```

> With no config present, fnack runs without a VPN exactly as before.

---

## Quick Start

### Docker Compose (Recommended)

Create `docker-compose.yml`:

```yaml
services:
  fnack:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: fnack
    restart: unless-stopped
    ports:
      - "4688:4688"
    volumes:
      - ./config:/config
      - ./downloads:/downloads
      - /path/to/music:/music
    environment:
      - SECRET_KEY=${SECRET_KEY:-}
      - MAX_CONCURRENT_DOWNLOADS=1
```

Build and run the container:

```bash
export MUSIC_PATH="/path/to/music"          # where your music library lives
export SECRET_KEY="$(openssl rand -hex 32)" # optional but recommended
docker compose up -d --build
```

Access the web interface at `http://<server-ip>:4688`.

> **Zero authentication required.** fnack resolves and downloads tracks without
> any Spotify, Tidal, Qobuz, or YouTube account. See [DEPLOY.md](DEPLOY.md) for
> the full production guide (upgrades, backups, troubleshooting).

---

## Configuration Settings

| Setting | Default | Description |
| :--- | :--- | :--- |
| `max_concurrent` | `1` | Maximum simultaneous track downloads |
| `spotiflac_delay` | `3.0s` | Pacing delay between SpotiFLAC processes to prevent 429 rate limiting |
| `youtube_source` | `youtube_music` | Prioritize official YouTube Music Topic releases vs general YouTube |
| `youtube_cookies_path` | `/config/cookies.txt` | Path to active Netscape cookies file for YouTube authentication |
| `music_path` | `/music` | Target directory for organized music library |
| `spotiflac_quality` | `LOSSLESS` | Lossless audio quality preference (LOSSLESS, HIGH, LOW) |
| `ytdlp_format` | `opus` | Fallback audio format (`opus`, `m4a`, `flac`, `mp3`) |
| `matching_strictness` | `standard` | Duration tolerance: `strict` (±4s), `standard` (±8s), `lenient` (±15s) |
| `enable_duration_check` | `true` | Set `false` to skip duration verification and accept any downloaded audio |

---

## Architecture

- **Backend**: Python 3.11+, Flask, Flask-SocketIO, Gevent WSGI, SQLAlchemy (SQLite in WAL mode)
- **Engines**: SpotiFLAC, yt-dlp, Mutagen, Watchdog, Chromium / Xvfb
- **Metadata Sources**: Deezer API, iTunes Search API, Spotify Metadata, MusicBrainz
- **Frontend**: ES6 JavaScript, Bootstrap 5.3, FontAwesome 6, CSS Custom Properties

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
