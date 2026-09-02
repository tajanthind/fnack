# fnack

Automated Lossless Music Discography Downloader, Tagger, and Library Manager.

[![Docker](https://img.shields.io/badge/docker-ready-blue.svg?logo=docker)](https://github.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg?logo=python)](https://python.org)

---

## What fnack is

**fnack** is a lightweight, self-hosted service that downloads, tags, and
organizes complete artist discographies into your music library. It runs as a
single container, requires no accounts, and works with your media server
(Navidrome, Plex, Jellyfin, Subsonic).

> **Zero authentication required.** fnack resolves and downloads tracks without
> any Spotify, Tidal, Qobuz, or YouTube account.

- **Lossless downloads with smart fallback** — true lossless FLAC when
  available; if a track can't be downloaded losslessly, fnack falls back to
  high-quality extraction from YouTube / YouTube Music.
- **One-click discography sync** — add an artist and fnack finds their albums
  and tracks, resolves each song, downloads it, tags it, and files it into
  your library.
- **Verified before it's accepted** — every download is checked against the
  official release (duration + embedded tags, plus optional fingerprinting)
  before it reaches your library; wrong-song or corrupted downloads are
  rejected automatically.
- **Library management** — overview stats, one-click retry of failed tracks,
  parallel folder import, manual match & track fixer, per-track monitoring.
- **Media-server friendly** — can trigger a rescan on your media server when
  downloads finish, and can expose your library as a Subsonic server for
  Symfonium, DSub, Sublime Music, etc.

---

## Quick Start

### Docker Compose (Recommended)

The repository ships a ready-to-use `docker-compose.yml` that works on any
machine — with or without git (a prebuilt image is published to GHCR on every
push; you can also build locally). First run:

```bash
cp .env.example .env          # then edit MUSIC_PATH to your music library
docker compose up -d          # pulls the prebuilt image (GHCR)
# or build locally:           docker compose up -d --build
```

Deploying to another machine without git is just as easy — see
[DEPLOY.md](DEPLOY.md#1b-deploy-on-another-machine--without-git) for the
one-file release bundle and offline options.

### Manual `docker run`

```bash
docker run -d --name fnack \
  -v ./config:/config -v ./downloads:/downloads -v /path/to/music:/music \
  -p 4688:4688 fnack:latest
```

Access the web interface at `http://<server-ip>:4688`.

---

## Configuration

Configuration is split into **core** settings (fnack itself) and
**plugin-owned** settings (each plugin's own preferences). Core settings:

| Setting | Default | Description |
| :--- | :--- | :--- |
| `max_concurrent` | `1` | Maximum simultaneous track downloads |
| `music_path` | `/music` | Target directory for organized music library |
| `matching_strictness` | `standard` | Duration tolerance: `strict` (±4s), `standard` (±8s), `lenient` (±15s) |
| `enable_duration_check` | `true` | Set `false` to skip duration verification and accept any downloaded audio |

Plugin settings are managed from **Settings → Plugins**: each plugin owns its
own settings, state, and cache — core has none. (Legacy flat settings from
older versions are read once as a migration fallback and moved into the owning
plugin; the plugin setting is authoritative.)

---

## Plugins

fnack is built on a small plugin system: the core orchestrates
**capabilities** — e.g. *download a track*, *search an artist*, *scan a media
server* — and plugins provide them. The Docker image ships with the plugins
needed for the default out-of-box experience (Spotify + YouTube downloads and
discography sync); everything else is optional.

- **Optional official plugins** — fingerprinting, media-server scans, VPN,
  notifications, Subsonic server, and more — are one click away in
  **Settings → Plugins → Marketplace**.
- **Community plugins** are installed the same way, can replace any official
  provider for the same capability, and need no core change.

See [docs/plugins/AUTHORING.md](docs/plugins/AUTHORING.md) for writing your
own plugins.

---

## Guides

- [YouTube `cookies.txt` Setup](docs/guides/youtube-cookies.md) — fix
  bot-checks / sign-in blocks on YouTube downloads.
- [VPN Setup (Optional)](docs/guides/vpn.md) — route downloads through a VPN
  tunnel running inside the container.

---

## Documentation

- [Deployment](DEPLOY.md)
- [Plugin authoring](docs/plugins/AUTHORING.md)
- [Architecture](docs/architecture.md) — for developers

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
