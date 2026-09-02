# fnack architecture

> **For developers.** The README is the user-facing document; this is the
> canonical architecture reference. Design records and the phase history live
> in [`wayfinder/`](../wayfinder/plugin-architecture-map.md).

## The one idea

fnack core is a thin orchestrator. Everything fnack *does* — download,
metadata, fingerprinting, verification, media-server integration — is provided
by **plugins** that implement **capabilities**. Core never imports a provider
implementation and never branches on a provider ID.

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

**Core is provider-free.** `queue_service.py` and the application services ask
the registry "who can do X?" and get back providers — core never imports a
provider implementation and never branches on a provider ID. fnack does not
know "SpotiFLAC" or "Deezer" as code; it knows the `download.track` and
`artist.search` capabilities.

## The rules that define the architecture

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

## Capabilities

The capability registry is the single mechanism for "who does X": every
capability is identified by a stable string, any number of enabled plugins may
implement it, and providers are tried in priority order. The runtime boundary
is the `ProviderExecutor` — application code always goes through
`PluginManager.invoke_provider`, never a raw provider call.

| Capability | What it provides |
|-----------|------------------|
| `download.track` | Download a track file |
| `track.resolve` | Resolve a Spotify URL to a track with ISRC |
| `track.metadata` / `album.metadata` / `album.tracks` | Track/album metadata |
| `artist.search` / `artist.discography` / `artist.info` | Artist search + discography |
| `album.search` / `track.search` | Album/track search |
| `fingerprint.identify` | Audio fingerprinting |
| `media.scan` / `media.health` / `media.connection_test` | Media-server scan + health |
| `library.task` | Maintenance tasks |
| `server.extension` | Expose a server API (Subsonic, Lidarr) |
| `auth.provider` | Optional authentication |
| `notification.event` | Download notifications |
| `network.route` | Network routing (VPN) |

## Official plugins

The official plugin catalog lives in the
[fnack-plugins](https://github.com/tajanthind/fnack-plugins) repository — its
`index.json` is the authoritative inventory; this repo's `bundled_plugins/`
is the vendored copy. Snapshot of the catalog at the time of writing:

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

Note the distinction between **media-server scan** (`media.scan`, the
`fnack.navidrome` provider triggers rescans on your server) and the
**Subsonic/OpenSubsonic server** (`server.extension`, `fnack.subsonic` exposes
your library as a server for clients) — two different concepts, two different
plugins.

## Essential vs optional packaging

The Docker image ships **only** the essential plugins — the set required for
the out-of-box first-run workflow: `fnack.spotiflac`, `fnack.ytdlp`,
`fnack.spotify`, `fnack.deezer-batch`.

- The **single source of truth** for this set is
  [`plugins/essential.py`](../plugins/essential.py) (`ESSENTIAL_PLUGINS`).
- The Docker build prunes `bundled_plugins/` to that set
  (`scripts/select_essential_plugins.py`), so startup auto-installs exactly
  the essential plugins.
- Every other official plugin remains installable from the fnack-plugins
  Marketplace (Settings → Plugins) and is fully supported; core has no code
  path that depends on any optional plugin being present.
- Maintenance tasks run as a core subprocess (`scripts/run_maintenance.py`),
  not via `library.task` — the library-task plugins are optional.

## Configuration model

Configuration is split into **core** settings (fnack itself, in the
"Core configuration" table of the README) and **plugin-owned** settings
(each plugin's preferences, edited from Settings → Plugins). Plugin settings,
state, and caches live inside the plugin — never in core. Legacy flat keys
(like `spotiflac_quality`, `ytdlp_format`, `youtube_cookies_path`) are read
once as a migration fallback and moved into the owning plugin's settings; the
plugin setting is authoritative.

## Plugin boundary

Plugins see only the public SDK (`fnack.plugin_api`) and `PluginContext` —
never `models`, `app`, or core internals. Plugins may call a small set of
generic core helpers (verifier policy, VPN infrastructure) but never provider
services. Multi-file plugins place sibling modules next to `plugin.py`; the
manager puts the plugin directory on `sys.path` so those imports resolve.

## Verification model

Verification is provider-neutral: duration (against the official release),
embedded tags, and fingerprint evidence are normalized into a
`VerificationResult` with no provider-specific rules in core. Fingerprinting
(`fingerprint.identify`) is optional — without an AcoustID API key it is a
silent no-op, and duration+tag verification still applies.
