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
| `server.extension` | Expose a server API (e.g. Lidarr-compatible grab API) |
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
| `fnack.deezer-batch` | `artist.search`, `artist.discography`, `artist.info`, `track.metadata`, `album.metadata`, `album.search`, `track.search`, `album.tracks` | Deezer metadata provider |
| `fnack.musicbrainz` | `artist.search`, `artist.discography` (+ `enrich`) | Catalogue enrichment |
| `fnack.itunes` | `artist.search`, `artist.discography`, `track.metadata`, `album.tracks` | iTunes metadata fallback |
| `fnack.spotify` | `track.resolve` | Spotify track-URL resolution (ISRC-first, zero-auth) |
| `fnack.acoustid` | `fingerprint.identify` | Audio fingerprinting / verification |
| `fnack.navidrome` | `media.scan`, `media.health`, `media.connection_test` | Media-server scan + health + split repair |
| `fnack.vpn` | `network.route` | Optional in-container VPN tunnel |
| `fnack.lidarr` | `server.extension` | Lidarr-compatible API |
| `fnack.discord-webhook`, `fnack.ntfy-webhook` | `notification.event` | Download notifications |
| `fnack.reverse-proxy-auth` | `auth.provider` | Optional header-based SSO (complements the built-in accounts) |
| `fnack.clean-navidrome-artists`, `fnack.normalize-album-tags`, `fnack.fix-navidrome-splits`, `fnack.reverify-library` | `library.task` | Maintenance tasks |

Note the distinction between **media-server scan** (`media.scan`, the
`fnack.navidrome` provider triggers rescans on your server) and the
**server API plugins** (`server.extension`, e.g. `fnack.lidarr` exposes a
Lidarr-compatible grab API) — two different concepts, two different plugins.

## Marketplace identity (multi-repository contract)

fnack supports any number of plugin repositories at once, and a plugin id is
**not globally unique**: the same id can be published by several
repositories with different content. The marketplace therefore has an
explicit, single contract — there is no implicit "which repo" rule anywhere:

- **Browse = one entry per (repository, plugin).** The Marketplace never
  merges duplicate ids across repositories and never picks a "newest" one.
  Every card names its source repository, and every card that duplicates an
  id from another enabled repository shows an explicit warning listing the
  other publishers.
- **Installs carry provenance.** Installing is always installing *from a
  repository*: the request includes `source_repo_id`, recorded on the
  `InstalledPlugin` row (`source_repo_id`). An install request *without* a
  source is refused as ambiguous whenever more than one enabled repository
  publishes the id — the candidates are listed, nothing is picked for you.
- **Updates follow provenance.** "Update" re-installs from the repository the
  plugin was originally installed from — never from whichever repository
  happens to be checked first. A duplicate of the same id in a second
  repository never drives the update badge or pulls a different fork in.
- **Switching sources is explicit.** Installing an id that is already
  installed from a different repository is a labelled "switch" action in the
  UI (and a plain re-install from the new source via the API); it is never a
  silent side effect of repository order.
- **Duplicates are detected, not ignored.** Adding or refreshing a repository
  that publishes an id also present in another enabled repository returns a
  conflict warning to the UI immediately; the Marketplace cards carry the
  same warning inline.
- Repository *order* and *insertion time* are never semantic: they affect
  nothing except the order cards are displayed in.

Consequences for plugin authors: reverse-DNS ids remain the recommended
convention (they make collisions unlikely), but the platform no longer
*relies* on that convention — collisions are visible, and the user picks the
source at install time.

## Accounts & authentication

The whole web app (pages and `/api/*`) is behind a login — there is no
anonymous access. On first boot, or whenever the database holds no user
accounts (a reset config volume), fnack serves only `/setup` until the
initial **admin** account is created; `/login` then guards everything else.

- Credentials are stored **salted scrypt hashes** (`services/accounts.py`,
  werkzeug) — never plaintext, never reversible; login compares in
  constant time.
- **Roles**: `admin` (account management) and `user` (everything else).
  Only the first account is admin; admins create/promote/delete accounts
  from Settings → user management.
- **Machine clients** keep working via the optional M2M API key
  (`X-API-Key`/`Authorization: Bearer`, Settings → shows the key) — the same
  key integrations used before the lockdown.
- **auth_provider plugins** (e.g. `fnack.reverse-proxy-auth`, header SSO) are
  an additional identity source for users already authenticated by a reverse
  proxy — an account must still exist.
- Sessions are signed with the persistent `SECRET_KEY`; cookies are
  HttpOnly + SameSite=Lax (`FNACK_COOKIE_SECURE=1` behind https), and
  cross-origin state changes from session-authenticated requests are refused
  (Origin check) as CSRF defence.
- `/health`, `/static` and the Socket.IO transport stay open (probes/UX);
  every data-bearing route requires an identity.

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

## Identity model (provider-scoped, provider-neutral)

Core stores each artist/album/track's **external identity** as an opaque
string alongside the **provider that supplied it**
(`Artist.provider_id` / `Album.provider_id` / `Track.provider_id` hold the
supplying plugin's id; `external_id` holds that provider's own id for the
entity). Core never parses, converts, or int()s either value — when the
provider chain resolves an entity, the selected provider interprets its own
identity (a Deezer provider converts a numeric Deezer id, a MusicBrainz
provider an MBID, and so on). Providers that don't recognize an id decline
it gracefully (`None`/empty), so the chain moves on; a foreign id never
crashes core.

Scoping by provider means two different providers may use the SAME external
id for different entities without colliding: artists are unique per
(provider_id, external_id), and discography dedup/prune happens inside the
serving provider's namespace. Search results carry the supplying provider,
and syncs record the provider that actually served the discography.
Self-created identities (e.g. `acoustid:<name>` created from a single
unknown track) have provider_id NULL and use a prefixed external id.

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
