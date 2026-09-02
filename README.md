# fnack

Automated Lossless Music Discography Downloader, Tagger, and Library Manager.

[![Docker](https://img.shields.io/badge/docker-ready-blue.svg?logo=docker)](https://github.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg?logo=python)](https://python.org)

---

## What fnack is

**fnack** is a lightweight, self-hosted service that downloads, tags, and
organizes complete artist discographies into your music library. It runs as a
single container, requires no accounts, and integrates with your media server
(Navidrome, Plex, Jellyfin, Subsonic).

Everything fnack does — download, metadata, fingerprinting, verification,
media-server integration — is built on **capabilities resolved through a
plugin registry**. The core is a thin orchestrator; the actual providers are
official plugins that ship bundled (and can be replaced by community plugins
or disabled entirely).

> **Zero authentication required.** fnack resolves and downloads tracks without
> any Spotify, Tidal, Qobuz, or YouTube account.

---

## Architecture

This is the single most important thing to understand about fnack:

```text
fnack core (queue, services, UI)
    │
    ▼
Application service        (DownloadService, MetadataService,
    │                        FingerprintService, VerificationService,
    ▼                        MediaServerService, ...)
Capability                 (download.track, artist.search, track.resolve,
    │                        fingerprint.identify, media.scan, ...)
    ▼
Provider registry          (enabled plugins, priority-ordered)
    │
    ▼
Provider plugin           (fnack.spotiflac, fnack.ytdlp, fnack.deezer-batch,
                             fnack.musicbrainz, fnack.itunes, fnack.spotify,
                             fnack.acoustid, fnack.navidrome, ...)
```

**Core is provider-free.** `queue_service.py` and the application services
ask the registry "who can do X?" and get back providers — core never imports
a provider implementation and never branches on a provider ID. fnack does not
know "SpotiFLAC" or "Deezer" as code; it knows the `download.track` and
`artist.search` capabilities.

### The rules that define the architecture

- **Providers are plugins.** Every provider ships as a plugin with its
  implementation, settings, state, and cache inside the plugin. Core contains
  no provider implementations.
- **A plugin can expose multiple capabilities.** e.g. `fnack.deezer-batch`
  serves `artist.search`, `artist.discography`, `artist.info`,
  `track.metadata`, `album.metadata`, `album.search`, `track.search`, and
  `album.tracks`.
- **Multiple plugins can implement the same capability.** e.g. `download.track`
  is served by both `fnack.spotiflac` (priority 10, primary) and
  `fnack.ytdlp` (priority 50, fallback); `artist.search` is served by
  `fnack.deezer-batch`, `fnack.musicbrainz`, and `fnack.itunes`.
- **Priority is per capability.** Each plugin has a manifest priority, and a
  plugin-level override; capability-specific priorities can differ from the
  plugin default. Lower numbers are tried first.
- **Disabling a plugin removes its capabilities.** A disabled plugin is not a
  provider — the registry simply stops returning it.
- **Zero providers is a valid state.** If no plugin supplies a capability,
  core returns a structured `CapabilityUnavailable` — it never falls back to
  a hidden implementation.
- **Official plugins are bundled for out-of-box behavior.** A normal install
  works immediately; disabling one only removes its capability.
- **Community plugins can replace official providers.** Disable an official
  plugin, install a community plugin implementing the same capability — core
  needs no source change.
- **Verification is provider-neutral.** Duration, embedded tags, and
  fingerprint evidence (AcoustID today, future providers) are combined by
  VerificationService into a normalized result. No provider-specific rules
  live in core.

### Official plugins

| Plugin | Capabilities | Role |
|--------|-------------|------|
| `fnack.spotiflac` | `download.track` | Primary lossless downloader (FLAC via Tidal/Qobuz/Deezer/SoundCloud, zero-auth) |
| `fnack.ytdlp` | `download.track` | Fallback downloader (YouTube / YouTube Music / SoundCloud) |
| `fnack.deezer-batch` | `artist.search`, `artist.discography`, `artist.info`, `track.metadata`, `album.metadata`, `album.search`, `track.search`, `album.tracks` | Authoritative Deezer metadata |
| `fnack.musicbrainz` | `artist.search`, `artist.discography` (+ `enrich`) | Catalogue enrichment |
| `fnack.itunes` | `artist.search`, `artist.discography`, `track.metadata`, `album.tracks` | iTunes metadata fallback |
| `fnack.spotify` | `track.resolve` | Spotify track-URL resolution (ISRC-first, zero-auth) |
| `fnack.acoustid` | `fingerprint.identify` | Audio fingerprinting / verification |
| `fnack.navidrome` | `media.scan`, `media.health`, `media.connection_test` | Media-server scan + health + split repair |
| `fnack.subsonic` | `server.extension` | Exposes your library as a Subsonic/OpenSubsonic server |
| `fnack.vpn` | `network.route` | Optional in-container VPN tunnel |
| `fnack.lidarr` | `server.extension` | Lidarr-compatible API |
| `fnack.discord-webhook`, `fnack.ntfy-webhook` | `notification.event` | Download notifications |
| `fnack.reverse-proxy-auth` | `auth.provider` | Optional header-based auth (zero-auth by default) |
| `fnack.clean-navidrome-artists`, `fnack.normalize-album-tags`, `fnack.fix-navidrome-splits`, `fnack.reverify-library` | `library.task` | Maintenance tasks |

Plugins are managed from **Settings → Plugins**: enable/disable, per-plugin
settings, priority, and (for community plugins) install/update/uninstall from
a repository. See [docs/plugins/AUTHORING.md](docs/plugins/AUTHORING.md) for
writing plugins.

---

## Features

Each feature below names the capability that provides it, and the plugin that
serves it by default.

### Downloads

- **Lossless primary + smart fallback** (`download.track`): `fnack.spotiflac`
  downloads true lossless FLAC; if it can't, `fnack.ytdlp` extracts from
  YouTube/YouTube Music. Both are providers of the same capability — disable
  one and the other still works.
- **ISRC-first resolution** (`track.resolve`): `fnack.spotify` matches tracks
  by International Standard Recording Code before falling back to title/artist
  search, with artist-aware verification of every candidate.
- **YouTube Music preference** (`fnack.ytdlp`): prioritizes official record
  label Topic master tracks, avoiding music videos with intros/dialogue.
- **`cookies.txt` support** (`fnack.ytdlp`): authenticate with YouTube to
  bypass bot-checks or sign-in blocks — upload via Settings or mount at
  `/config/cookies.txt`.
- **Rate limiting & retries**: configurable inter-request pacing and automatic
  exponential backoff eliminate upstream 429 throttling when adding large
  batches.

### Library management

- **Global overview stats**: artists managed, tracks downloaded, failed songs,
  and total catalogue size (MB/GB/TB).
- **One-click global retry**: re-queue every failed or error track.
- **Parallel folder import** (`/import`): scan your `/music` root
  concurrently and import discovered artist folders in one batch, with a live
  progress bar.
- **Manual match & track fixer**: fetch any missing/failed song from a
  specific Spotify, YouTube, YouTube Music, or Deezer link; the targeted
  stream is downloaded, tagged, and replaces the incorrect file.
- **Tagging & organization**: multi-disc filename numbering, tag
  normalization, `cover.jpg` artwork saved + embedded (FLAC Picture, ID3 APIC,
  MP4 covr).
- **Monitoring**: toggle per-track or per-album monitoring.

### Verification & quality

- **Duration profiles** (verifier + `fnack.acoustid`): strict (±4s),
  standard (±8s), and lenient (±15s) duration matching filter out live
  versions, karaoke, and video sketches.
- **Fingerprint confirmation** (`fingerprint.identify`): `fnack.acoustid`
  confirms "right file, wrong tags" downloads, flags different-song files for
  your decision, and identifies unknown/regional tracks.
- **Provider-neutral verification**: embedded tags + duration + fingerprint
  evidence are combined by VerificationService — no provider-specific rules in
  core.

### Media-server integration

Two different things, provided by two different plugins:

- **Media-server scan** (`media.scan`): `fnack.navidrome` triggers
  `startScan.view` on your Navidrome server when downloads finish, exposes
  health/connection tests, and repairs split albums.
- **Subsonic/OpenSubsonic server** (`server.extension`): `fnack.subsonic`
  exposes your fnack library as a Subsonic server for Symfonium, DSub,
  Sublime Music, etc. — independent of the Navidrome provider.

---

## Configuration

Configuration is split into **core** settings (fnack itself) and
**plugin-owned** settings (each provider's own preferences, managed from
Settings → Plugins). Provider settings never live in core.

### Core configuration

| Setting | Default | Description |
| :--- | :--- | :--- |
| `max_concurrent` | `1` | Maximum simultaneous track downloads |
| `music_path` | `/music` | Target directory for organized music library |
| `matching_strictness` | `standard` | Duration tolerance: `strict` (±4s), `standard` (±8s), `lenient` (±15s) |
| `enable_duration_check` | `true` | Set `false` to skip duration verification and accept any downloaded audio |

### Plugin configuration

Each provider plugin owns its settings, exposed in Settings → Plugins:

| Plugin | Settings |
|--------|----------|
| `fnack.spotiflac` | `quality` (`LOSSLESS`/`HIGH`/`LOW`), `delay` (pacing, 0.5–5.0s), `timeout` |
| `fnack.ytdlp` | `format` (`opus`/`m4a`/`flac`/`mp3`), `audio_source` (`youtube`/`youtube_music`), `cookies_file` (upload), `cookies_path`, `timeout` |
| `fnack.spotify` | `client_id`, `client_secret` (optional official API path; zero-auth search works without them) |
| `fnack.acoustid` | `api_key` (optional; keyless = fingerprinting disabled, silent no-op) |
| `fnack.navidrome` | `url`, `user`, `token`, `auto_scan`, `db_path` |

> Legacy keys like `spotiflac_quality`, `ytdlp_format`, `youtube_cookies_path`
> are read once as a migration fallback and moved into the plugin's own
> settings; the plugin setting is authoritative.

---

## YouTube `cookies.txt` Setup Guide

If YouTube restricts downloads or requires sign-in, you can provide a `cookies.txt` file to fnack (the `fnack.ytdlp` plugin owns it).

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
fix is to route fnack through a **VPN** (the `fnack.vpn` plugin). Residential VPN IPs are flagged
far less often than datacenter IPs. fnack runs the VPN **inside its own container** — no extra
container needed.

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

The repository ships a ready-to-use `docker-compose.yml` that works on any
machine — with or without git (prebuilt image is published to GHCR on every
push; you can also build locally with `--build`). First run:

```bash
cp .env.example .env          # then edit MUSIC_PATH to your music library
docker compose up -d          # pulls the prebuilt image (GHCR)
# or build locally:           docker compose up -d --build
```

Deploying to another machine without git is just as easy — see
[DEPLOY.md](DEPLOY.md#1b-deploy-on-another-machine--without-git) for the
one-file release bundle (`./scripts/make_release.sh`) and offline options.

If you prefer to write the compose yourself, the equivalent minimal file:

```yaml
services:
  fnack:
    image: ghcr.io/tajanthind/fnack:latest
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
      - ${MUSIC_PATH:-./music}:/music
    environment:
      - SECRET_KEY=${SECRET_KEY:-}
      - MAX_CONCURRENT_DOWNLOADS=1
```

Access the web interface at `http://<server-ip>:4688`.

---

## Plugins

Official plugins ship bundled and are enabled by default. To disable one,
disable a capability, install a community replacement, or write your own, use
**Settings → Plugins** or see [docs/plugins/AUTHORING.md](docs/plugins/AUTHORING.md).

Community plugins are installed from a repository (add a repo URL in
Settings → Plugins → Repositories), sha256-verified, and can replace official
providers for any capability — core needs no source change.

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
