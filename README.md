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

### Dual Download Pipeline
- **SpotiFLAC (Primary)**: Downloads 16-bit and 24-bit FLAC audio streams.
- **yt-dlp (Fallback)**: Extracts YouTube Music Topic streams. Supports native **Opus (160 kbps)** and **AAC M4A (128 kbps)** with zero transcoding, or conversion to FLAC / MP3.
- **Configurable Engines**: Enable or disable either engine independently in Settings.

### Discography Filtering
- **Compilation and Playlist Filtering**: Excludes multi-artist label collections, DJ mixes, bootlegs, and party playlists.
- **Track-Level Artist Ratio Validation**: Multi-artist compilations where the target artist only appears on minor tracks are excluded from the main discography.
- **Deduplication**: Reuses existing audio files across shared tracks and features without re-downloading.

### Tagging & Storage Organization
- **Multi-Disc Numbering**: Automatically formats filenames as `{disc}-{track}. {title}.<ext>` to prevent multi-disc overwrites.
- **Tag Normalization**: Strips conflicting casing tags (`ALBUMARTIST`, `AlbumArtist`, `albumartist`) for consistent single-album grouping in Navidrome.
- **Cover Artwork**: Automatically saves `cover.jpg` / `folder.jpg` in album folders and embeds artwork into audio files (FLAC Picture blocks, ID3 APIC, MP4 covr).
- **Subsonic API Integration**: Automatically triggers `startScan.view` on your Navidrome server when downloads finish.

### Song Identification & Strictness
- **ISRC Matching**: Prioritizes exact International Standard Recording Code matching.
- **Duration Profiles**: Strict (±4s), Standard (±8s), and Lenient (±15s) duration matching to filter out live versions, karaoke, or video sketches.
- **Track/Album Monitoring**: Toggle monitoring on individual tracks or complete albums.

### Real-Time Library Monitoring & Import
- **Filesystem Watcher**: Background service monitors `/music` for file renames, moves, or deletions, updating the database in real time.
- **Interactive Import**: Scan existing local music folders, rank artist candidates, and manually map or search Deezer profiles before ingestion.

### Responsive UI & Themes
- Mobile-optimized interface with bottom navigation and responsive track tables.
- 6 built-in color themes (3 Dark: Onyx Red, Midnight Ocean, Emerald Cyber; 3 Light: Nordic Frost, Warm Amber, Rose Latte).

---

## Quick Start

### Docker Compose (Recommended)

Create `docker-compose.yml`:

```yaml
services:
  fnack:
    image: ghcr.io/YOUR_GITHUB_USERNAME/fnack:latest
    # Or build locally from source:
    # build: .
    container_name: fnack
    restart: unless-stopped
    ports:
      - "4688:4688"
    volumes:
      - ./config:/config
      - ./downloads:/downloads
      - /path/to/music:/music
    environment:
      - MAX_CONCURRENT_DOWNLOADS=3
      - SECRET_KEY=replace_with_random_secret_string
```

Run the container:

```bash
docker compose up -d
```

Access the web interface at `http://<server-ip>:4688`.

---

## Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Maximum simultaneous track downloads |
| `CONFIG_DIR` | `/config` | Path to persistent application database and settings |
| `MUSIC_PATH` | `/music` | Target directory for organized music files |
| `SECRET_KEY` | *(Auto-generated)* | Cryptographic session signing key |
| `CHROME_PATH` | `/usr/bin/chromium` | Browser executable path for headless tasks |

---

## Architecture

- **Backend**: Python 3.11+, Flask, Flask-SocketIO, Gevent WSGI, SQLAlchemy (SQLite in WAL mode)
- **Engines**: SpotiFLAC, yt-dlp, Mutagen, Watchdog
- **Metadata Sources**: Deezer API, iTunes Search API, Spotify Metadata, MusicBrainz
- **Frontend**: ES6 JavaScript, Bootstrap 5.3, FontAwesome 6, CSS Custom Properties

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
