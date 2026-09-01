# Writing fnack plugins (author guide)

This guide is for **plugin authors** — people who want to build plugins for
fnack. You do **not** need to read `PLUGIN_ARCHITECTURE.md` or `INTEGRATION.md`
(internal design docs) to build a plugin; everything you need is here.

fnack plugins are small Python packages dropped into a folder. They run
in-process, they are loaded at startup, and they only ever see a narrow
`PluginContext` — never fnack's database models, Flask app, or internal
services. That boundary is what lets plugins keep working across fnack
updates.

---

## 1. Quickstart — your first plugin in under a page

A plugin is a folder with two files:

```
/config/plugins/com.example.my-plugin/
├── plugin.json     # manifest (who you are, what you do)
└── plugin.py       # your code
```

The smallest useful plugin subscribes to an event and does something:

```json
{
  "id": "com.example.my-plugin",
  "name": "My First Plugin",
  "version": "1.0.0",
  "type": ["event_hook"],
  "api_version": "^1.0",
  "min_core_version": "0.2.0",
  "entry_point": "plugin:MyPlugin",
  "author": "You",
  "description": "Logs a line when a track finishes downloading.",
  "permissions": []
}
```

```python
from plugins.base import EventHookPlugin


class MyPlugin(EventHookPlugin):

    def on_load(self):
        # Subscribe to a core event. Core emits these from the download pipeline.
        self.context.events.subscribe("track.after_download", self._on_downloaded)

    def _on_downloaded(self, track_id: int, **_kwargs):
        self.context.log.info("Track %s finished downloading!", track_id)
```

To install it manually:

1. Create the folder `/config/plugins/com.example.my-plugin/` (inside fnack's
   config volume).
2. Put `plugin.json` and `plugin.py` in it.
3. Restart fnack. The plugin is discovered on boot and appears in
   Settings → Plugins; **enable it there once** so the enabled state persists
   (an enable creates the `InstalledPlugin` row that survives restarts).

On boot you'll see `fnack.plugins.manager` log lines about loading
`com.example.my-plugin`. When any track finishes downloading, your `on_load`
subscription fires and logs the line.

A fully-worked example ships with fnack at
`examples/plugins/example-quality-flag/` — it flags low-bitrate tracks with a
caution reason and contributes a badge to the track row. Copy it to start.

---

## 2. Manifest reference (`plugin.json`)

| Field | Required | Type | Meaning |
|---|---|---|---|
| `id` | yes | string | Reverse-DNS unique id, e.g. `com.example.my-plugin`. Collides across repos otherwise. |
| `name` | yes | string | Human-readable name shown in the UI. |
| `version` | yes | string | Semver, e.g. `1.2.0`. |
| `type` | yes | string or list | One or more plugin types — see the type table below. |
| `api_version` | yes | string | The fnack plugin API range you target. Use `^1.0` (means `>=1.0,<2.0`). |
| `entry_point` | yes | string | `module:ClassName` — e.g. `plugin:MyPlugin` loads `plugin.py` and instantiates `MyPlugin`. |
| `min_core_version` | no | string | Oldest fnack you support, e.g. `"0.2.0"`. fnack refuses to load below this. |
| `author` | no | string | Your name / handle. |
| `description` | no | string | One-line description, shown in the marketplace. |
| `homepage` | no | string | URL for more info / issues. |
| `permissions` | no | list | What your plugin may touch — see the permissions table. |
| `settings_schema` | no | list | Declares your settings form (auto-generated UI). |
| `capabilities` | no | list | Capability IDs this plugin provides (Phase 1, MASTER), e.g. `["download.track", "track.resolve"]`. Multiple allowed. When omitted, capabilities are derived from `type` (a `downloader` implies `download.track`, a `metadata_provider` implies `artist.search`/`artist.discography`/`track.metadata`/`album.metadata`, etc.). Unknown IDs warn (forward-compatible) rather than fail. |
| `ui` | no | dict | `{"slots": ["track_row_actions", ...]}` — which UI slots you contribute to. |
| `dependencies` | no | dict | `{"python": ["somelib>=2.0"]}` — Python deps (installed into a private area in a later release). |
| `trust_level` | no | string | `"official"`, `"verified"`, or `"community"` (default `"community"`). Only fnack's own bundled plugins use `official`. |

### Valid `type` values

| Type | What it does | You implement |
|---|---|---|
| `downloader` | Fetch a track's audio from a source | `can_handle`, `download`, `is_rate_limited` |
| `metadata_provider` | Search / enrich artist, album, track metadata | `search_artist`, `get_artist_discography`, `get_track_info` |
| `lyrics_provider` | Look up lyrics (planned sibling of metadata_provider) | *interface lands with implementation* |
| `fingerprint` | Acoustic identification / verification | `identify` |
| `scan_trigger` | Tell a media server to rescan | `trigger_scan`, `test_connection` |
| `library_task` | Maintenance / cleanup jobs, manual or scheduled | `run` (+ optional `schedule`) |
| `vpn` | Tunnel management | `start`, `stop`, `status` |
| `storage_backend` | Where finished files land (planned) | *interface lands with implementation* |
| `server_extension` | Register brand-new HTTP routes | `register_routes` |
| `ui_extension` | Contribute UI into a named slot | `render_slot` (or purely declarative via `ui.slots`) |
| `event_hook` | React to core events, no other interface | nothing required — subscribe in `on_load` |
| `auth_provider` | SSO / reverse-proxy auth (planned, Phase 4) | *interface lands with implementation* |
| `library_source` | Source of artists/albums to monitor (the mirror of `downloader`) | `list_artists` (+ optional `poll`) — see `fnack.lidarr` |
| `conflict_resolver` | Decide between duplicate/conflicting files (planned) | *interface lands with implementation* |
| `recommendation` | Suggest artists/albums/tracks (planned) | *interface lands with implementation* |

A plugin can be more than one type (e.g. `["scan_trigger", "ui_extension"]`).

### Permissions

Declared permissions gate what the `context.*` facades will do. Using a
facade without declaring its permission raises `PermissionError`.

| Permission | Gates |
|---|---|
| `network` | `context.http` requests (outbound network). |
| `settings` | `context.settings` read/write (per-plugin key/value store). |
| `filesystem:downloads` | `context.fs.open_download_path(...)` writes under the downloads dir. |
| `filesystem:music` | `context.fs` access to the music library dir. |

Declaring a permission you don't use shows a warning; using one you didn't
declare is blocked.

### `settings_schema` entries

Each entry: `{"key": "...", "type": "string|number|boolean|select|secret",
"default": ..., "required": true|false}` and for `select` an `options` list.
`secret: true` renders a password field and is stored encrypted-ish (never
echoed back in full).

### Capabilities (Phase 1, MASTER)

Capabilities are provider-neutral contracts between core and plugins. Core
asks "who provides `download.track`?" — it never asks "is SpotiFLAC
installed?". A plugin is the distribution unit; capabilities are runtime
contracts, and **one plugin can provide many capabilities**:

```json
{
  "id": "fnack.navidrome",
  "type": ["scan_trigger"],
  "capabilities": ["media.scan", "media.health", "media.connection_test"]
}
```

Known capability IDs:

```text
download.track          download.batch
track.resolve           track.metadata
artist.search           artist.discography     album.metadata
fingerprint.identify
media.scan              media.health           media.connection_test
library.task            server.extension       auth.provider
notification.event      network.route
```

Rules that matter for authors:

- Declaring a capability is **data, not behavior** — nothing special happens
  until core or another plugin actually queries the registry.
- Omitting `capabilities` is fine: they're derived from your `type`
  (a `downloader` implies `download.track`, a `metadata_provider` implies
  `artist.search`/`artist.discography`/`track.metadata`/`album.metadata`, a
  `scan_trigger` implies `media.scan`/`media.connection_test`, a `vpn`
  implies `network.route`, a `server_extension` implies
  `server.extension`, an `auth_provider` implies `auth.provider`, an
  `event_hook` implies `notification.event`). Declare explicitly when you
  provide something beyond the type default (e.g. `fnack.spotify` declares
  `track.resolve`).
- A disabled or uninstalled plugin's capabilities **disappear** — core must
  not silently fall back to a hidden implementation (that's the point).
- **Priorities stay core, and are capability-specific (Phase 1.1).** When
  several plugins provide the same capability, the registry orders them by
  that capability's effective priority (LOWEST number = tried first,
  deterministic tie-break by plugin_id). Resolution chain:
  `capability-specific override > plugin-level priority_override > manifest
  priority`. One plugin can serve different capabilities at different
  priorities (e.g. `track.resolve` at 5 and `track.metadata` at 30) without
  multiple instances. The per-plugin Priority field in Settings → Plugins
  sets the plugin-level default; per-capability overrides are configured via
  the API (`GET /api/plugins/<id>/capabilities`,
  `POST /api/plugins/<id>/capabilities/<cap>/priority`) — UI follow-up
  documented in the Phase 1.1 wayfinder ticket.
- **Declared capabilities are validated (Phase 1.1).** The runtime checks
  each declared capability against its contract
  (`fnack/plugin_api/contracts.py` — the single source of truth for
  capability → required interface methods). A capability the plugin doesn't
  actually implement is SKIPPED with a clear warning (plugin id, capability
  id, missing method); the plugin's other capabilities still load. Never
  declare a capability you don't implement — you'll get a warning and it
  won't register.

### The public SDK (`fnack.plugin_api`)

Phase 1 formalized a stable public API package that plugins should import
instead of reaching into `plugins.*` internals:

```python
from fnack.plugin_api import (
    DOWNLOAD_TRACK, TRACK_RESOLVE, MEDIA_SCAN,        # capability IDs
    TrackRef, DownloadRequest, DownloadResult,         # domain models
    TrackDownloader, FingerprintProvider,              # provider protocols
    ProviderExecutor,                                  # sync/async invocation
    CapabilityUnavailable, ProviderError, PluginError, # errors
    PluginContext, EventBus,                           # context + events
)
```

- `fnack.plugin_api.models` re-exports the existing model classes
  (`TrackRef`, `DownloadResult`, ...) — the same classes every plugin
  already uses, no duplicates.
- `fnack.plugin_api.providers` defines the small capability-oriented
  protocols (`TrackDownloader`, `TrackResolver`, `FingerprintProvider`,
  `MediaScanner`, `LibraryTaskProvider`, `ServerExtension`, `AuthProvider`,
  `NotificationProvider`, `NetworkRouter`) and `ProviderExecutor`, the one
  place that runs provider methods — sync or async (awaitables are detected
  with `inspect.isawaitable`; never call `asyncio.run()` in your plugin).
- **Runtime invocation boundary (Phase 1.1).** Application services call
  providers through `PluginManager.invoke_provider(...)` — never a raw
  method call and never `manager.executor.run(...)` directly. The manager's
  boundary routes through ProviderExecutor AND applies the gevent timeout +
  consecutive-failure + auto-disable + health guard, so every provider path
  gets the same safety. `invoke_provider` accepts either a LoadedPlugin or a
  provider instance.
- **Two contracts, explicitly (Phase 1.1).** The runtime VALIDATES the
  CURRENT COMPATIBILITY CONTRACT (`fnack/plugin_api/contracts.py` — the
  actual FNACK method names like `can_handle(track)` / `resolve_track_url`),
  while `providers.py` protocols describe the FINAL SDK CONTRACT (request-
  object signatures). Phase 2 bridges them with a migration adapter; do NOT
  rewrite plugins to the new protocol signatures as a 1.1 cleanup.
- Errors: raise or handle `CapabilityUnavailable` (no enabled plugin
  provides a capability — a *valid* state, not a bug) and `ProviderError`
  (a specific provider failed, with a stable `code` and `retryable` flag).

**SDK boundary (Phase 1.1 review).** `models.py`, `context.py`, and
`events.py` are currently TRANSITIONAL re-exports of internal
`plugins.base`/`plugins.context`/`plugins.events` classes, not yet standalone
public contracts. That is documented technical debt: plugins may keep using
them (they import cleanly and never pull in app services or provider
implementations), and the contracts become fully standalone during Phase 2
provider extraction. `capabilities.py`, `providers.py`, `errors.py`, and
`contracts.py` are real, standalone public contracts today.

**Phase 2: provider extraction (PR 3 = DownloadService + SpotiFLAC).**
`fnack.spotiflac` is the first authoritative provider: its implementation
moved into the plugin (`spotiflac.py` beside `plugin.py` — the plugin dir is
on `sys.path` so sibling modules import by name), and the plugin implements
the FINAL SDK `TrackDownloader` contract (request-object based, async). The
queue's DownloadService has a **migration adapter**: a provider implementing
the SDK contract is invoked with a `DownloadRequest` and its SDK
`DownloadResult` is normalized to the legacy `success/file_path/error` shape
the verification code consumes; legacy providers (e.g. `fnack.ytdlp`, until
PR 4) keep the old signature. Core no longer imports
`services.spotiflac_service` (deleted); the manual-download path routes
through the provider boundary; `vpn_service` emits `network.route_changed`
instead of importing the provider (the plugin owns its 429 circuit breaker
and resets it on that event). Provider settings are plugin-owned
(`quality`/`delay`/`timeout`) with the legacy global as a one-time fallback
that migrates into the plugin store.

**Phase 2 (PR 4): yt-dlp extraction.** `fnack.ytdlp` is the second
authoritative provider: the whole engine moved verbatim into the plugin
(`ytdlp.py` — CLI invocation, candidate scoring, YouTube Music preference,
cookies handling, format selection, errors) and `services/ytdlp_service.py`
+ `services/spotdl_service.py` (legacy alias) are deleted. The plugin
implements the same FINAL SDK `TrackDownloader` contract as spotiflac.
`DownloadRequest` gained provider-neutral hints the adapter carries:
`query` (raw URL/search string from the manual-download path), `cookies_path`,
`audio_source` (`youtube` vs `youtube_music`), and `check_duration` (the
queue verifies after download, so the provider may skip its internal check).
The `engine_gates` dict (provider-ID-keyed toggles like `enable_ytdlp`) is
REMOVED from the queue chain: core never names a provider or checks a
provider-specific toggle — the download.track capability registry is the
ONLY enable/disable mechanism (a disabled provider plugin simply isn't in
`get_downloaders()`).
Generic core helpers (audio-file verification, AcoustID matching) reach the
plugin only through the PluginContext facade (`library.verify_audio_file` /
`library.verify_download_acoustid`) — the plugin never imports `services.*`;
a lazy guarded fallback keeps `ytdlp.py` usable standalone in tests. The
cookies settings UI routes through the provider boundary too: `app.py` duck-
types any enabled `download.track` provider exposing `get_cookies_status` /
`get_cookies_path`, with minimal core fallbacks. Legacy `ytdlp_format` /
`spotdl_format` / `spotdl_source` / `youtube_cookies_path` globals are a
one-time fallback migrated into the plugin store in `on_load` (full legacy-
setting/UI deletion is deferred to PR 11/12).

---

## 3. One section per plugin type

All types extend `PluginBase` (lifecycle: `on_load`, `on_enable`,
`on_disable`, `on_unload`, `on_settings_changed`) and are constructed with a
`PluginContext`. You only implement the methods your type needs.

### `downloader`

```python
from plugins.base import DownloaderPlugin, DownloadResult


class MyDownloader(DownloaderPlugin):
    priority = 50  # lower runs first; fnack tries providers in ascending order

    def can_handle(self, track) -> bool:
        # Cheap pre-check, NO network calls. Return True if you can fetch this track.
        return bool(track.isrc) or bool(track.spotify_url)

    def download(self, track, dest_dir, options) -> DownloadResult:
        # fetch audio into dest_dir, return the resulting file
        file = dest_dir / f"{track.title}.flac"
        # ... download logic ...
        return DownloadResult(success=True, file_path=file, extra={"format": "flac"})

    def is_rate_limited(self) -> bool:
        # Return True while an upstream rate limit / circuit breaker is open.
        return False
```

`track` is a `TrackRef` dataclass: `id`, `title`, `artist_name`, `album_name`,
`isrc`, `duration`, `spotify_url`, `deezer_id`, `disc_number`,
`track_number` — read-only, no ORM.

**Priority is user-adjustable.** The `priority` class attribute is the
manifest default. Users can override it per-install from Settings → Plugins
(the numeric input writes `InstalledPlugin.priority_override`); fnack sorts
by the override when set, falling back to your declared priority. Overrides
persist across restarts.

### `metadata_provider`

```python
from plugins.base import MetadataProviderPlugin


class MyProvider(MetadataProviderPlugin):
    priority = 100

    def search_artist(self, name: str) -> list[dict]:
        return [{"id": "abc", "name": name, "image_url": None}]

    def get_artist_discography(self, provider_artist_id: str) -> dict:
        return {"artist_name": ..., "albums": [...]}

    def get_track_info(self, provider_track_id: str):
        return None  # optional
```

Providers run in ascending `priority` order; fnack stops at the first one that
returns a useful answer for the current job.

### `fingerprint`

```python
from plugins.base import FingerprintPlugin, FingerprintResult


class MyFingerprinter(FingerprintPlugin):
    def identify(self, file_path) -> FingerprintResult:
        return FingerprintResult(confidence=0.95, matched_title="Song",
                                 matched_artist="Artist")
```

### `scan_trigger`

```python
from plugins.base import ScanTriggerPlugin


class MyScanner(ScanTriggerPlugin):
    def trigger_scan(self) -> tuple[bool, str]:
        return True, "scan started"

    def test_connection(self) -> tuple[bool, str]:
        return True, "connected"
```

### `library_task`

```python
from plugins.base import LibraryTaskPlugin, TaskResult


class MyCleanup(LibraryTaskPlugin):
    schedule = "daily"  # or "hourly", or None for manual-only

    def run(self) -> TaskResult:
        return TaskResult(success=True, message="cleaned 3 files")
```

`schedule` accepts `None` (manual only), `"hourly"`, `"daily"`, or a cron-ish
string. Manual tasks are triggered from the Maintenance panel.

### `vpn`

```python
from plugins.base import VPNPlugin


class MyVPN(VPNPlugin):
    def start(self) -> tuple[bool, str]: return True, "up"
    def stop(self) -> tuple[bool, str]: return True, "down"
    def status(self) -> dict: return {"running": False, "ip": None}
```

### `server_extension`

```python
from plugins.base import ServerExtensionPlugin


class MyApi(ServerExtensionPlugin):
    def register_routes(self, blueprint) -> None:
        @blueprint.route("/my-api/hello")
        def hello():
            return {"hello": "world"}
```

### `ui_extension`

```python
from plugins.base import UIExtensionPlugin


class MyWidget(UIExtensionPlugin):
    def render_slot(self, slot_name: str, context_data: dict) -> str:
        return '<span class="badge bg-info">Hello</span>'
```

### `event_hook`

No required methods. Subscribe to events in `on_load` (see Quickstart). This
is the type for notifications/webhooks/cross-cutting flags.

---

## 4. `PluginContext` reference

Every plugin instance holds `self.context`. These are the ONLY capabilities
you get. There is no `db`, no `app`, no `models` import.

| Facade | Method / attr | What it does | Permission needed |
|---|---|---|---|
| `context.library` | `get_track(track_id) -> dict\|None` | Read a track (id, title, isrc, status, file_path, duration, bitrate, caution, caution_info). | — |
| `context.library` | `get_album(album_id) -> dict\|None` | Read an album (id, name, year, is_downloaded). | — |
| `context.library` | `get_artist(artist_id) -> dict\|None` | Read an artist (id, name, monitored). | — |
| `context.library` | `list_missing_tracks(limit=500) -> list` | Tracks with status "missing". | — |
| `context.library` | `list_artists() -> list` | All artists (id, name, image_url). | — |
| `context.library` | `list_albums(artist_id=None, limit=500) -> list` | Albums, optionally filtered by artist (id, name, year, artist_id, cover_url, is_downloaded). | — |
| `context.library` | `list_tracks(album_id=None, limit=1000) -> list` | Tracks, optionally filtered by album (id, title, track_number, disc_number, duration, file_path, local_path, is_downloaded, bitrate, size_bytes). | — |
| `context.library` | `get_api_key() -> str` | The configured M2M API key (`''` if unset — zero-auth model means unset = open). | — |
| `context.library` | `get_or_create_api_key() -> str` | The M2M API key, generating + persisting one if none is set (Lidarr-style integrations authenticate against this). | — |
| `context.library` | `update_track_status(track_id, status, error_message=None)` | Set a track's status (and optional error). | — |
| `context.library` | `mark_caution(track_id, reason)` | Flag a track for user attention (badge in the UI); does not change status or delete. | — |
| `context.library` | `search_albums(query, limit=10) -> list` | Live Deezer album search (same function the interactive search endpoint uses). | — |
| `context.library` | `search_tracks(query, limit=10) -> list` | Live Deezer track search (same function the interactive search endpoint uses). | — |
| `context.library` | `get_album_info(album_id) -> dict` | Deezer album metadata (core-direct; e.g. friendly release names). | — |
| `context.library` | `get_track_info(track_id) -> dict` | Deezer track metadata (core-direct). | — |
| `context.library` | `queue_lidarr_grab(item_type, item_id) -> list[int]` | Expand a Lidarr grab (Deezer album/track id) into Artist/Album/Track rows + queued DownloadJobs. Returns job ids. | — |
| `context.library` | `list_download_jobs(statuses) -> list` | DownloadJobs in the given statuses (id, album_name, status, progress, artist_name, source). | — |
| `context.library` | `cancel_download_job(job_id) -> bool` | Cancel a DownloadJob. | — |
| `context.settings` | `get(key, default=None)` | Read your plugin's persisted setting. | `settings` |
| `context.settings` | `set(key, value)` | Write your plugin's persisted setting. | `settings` |
| `context.settings` | `all() -> dict` | All your plugin's settings. | `settings` |
| `context.events` | `subscribe(event, fn)` | Listen for a core event. Auto-untangled on disable. | — |
| `context.events` | `emit(event, **payload)` | Emit an event other plugins/core can hear. | — |
| `context.http` | `requests.Session` | Preconfigured session (timeouts + fnack UA) for outbound HTTP. | `network` |
| `context.fs` | `downloads_dir`, `music_dir` | Paths to the download work dir and the music library. | — |
| `context.fs` | `data_dir` | Your plugin's private scratch dir (auto-created). | — |
| `context.fs` | `open_download_path(relative)` | Resolve a path under downloads. | `filesystem:downloads` |
| `context.fs` | `open_data_path(relative)` | Resolve a path under your private data dir. | — |
| `context.ui` | `register_slot(slot, render_fn)` | Contribute HTML to a UI slot (render_fn(context_data) -> str). | — |
| `context.jobs` | `schedule_interval(seconds, fn)` | Run a function on an interval. | — |
| `context.log` | logging.Logger | Logger namespaced `fnack.plugin.<your-id>`. | — |

Core events you can subscribe to:

```
track.before_download   track.after_download   track.verified   track.caution_flagged
album.imported          artist.added            artist.synced
library.scan_requested  queue.job_completed     queue.job_failed
maintenance.run
```

---

## 5. UI slots

Templates call `{{ plugin_slot('slot_name', **data) }}`. Core loops over every
enabled plugin that registered that slot and concatenates the fragments.

| Slot | Where it renders | `context_data` passed |
|---|---|---|
| `track_row_actions` | Each track row on the artist page | `{"track": {...}}` (caution, caution_info, id, title...) |
| `settings_tab` | Settings page | `{}` (page-level) |
| `dashboard_widget` | Home page stats area | `{}` |
| `nav_item` | Top navigation | `{}` |
| `queue_item_actions` | Queue page | `{}` |

Worked template — a badge that appears on tracks you flagged:

```python
def _render_badge(self, context_data: dict) -> str:
    track = context_data.get("track") or {}
    if not track.get("caution"):
        return ""
    return f'<span class="badge bg-warning text-dark" title="{track.get("caution_info", "")}">⚠ My Flag</span>'
```

```json
{ "ui": { "slots": ["track_row_actions"] } }
```

In `on_load`: `self.context.ui.register_slot("track_row_actions", self._render_badge)`.

Never render raw/unescaped user input into slot HTML.

---

## 6. Local development loop

1. Write your plugin in `/config/plugins/<id>/` (manual install).
2. Restart fnack. Load failures are logged as `fnack.plugins.manager` errors —
   check `docker logs fnack` or your journal.
3. Every call into your plugin is wrapped by fnack with a timeout (default 10s)
   and an exception guard. If your plugin throws or hangs repeatedly (5
   consecutive failures) it is **auto-disabled** with an entry in the plugin
   health log — the app never crashes because of you.
4. While disabled, your `on_disable()` runs (release timers/sockets). Fix the
   bug, then re-enable from Settings → Plugins.
5. **Settings → Plugins** (`/plugins` page) lists installed plugins grouped by
   type, with name/version/trust badge/enabled status/health and an
   enable/disable toggle. A manually-installed plugin appears there once
   discovered; enabling it persists an `InstalledPlugin` row so the enable
   survives restart. Downloaders/metadata providers also show a numeric
   **priority** input (writes `priority_override`; empty = manifest default).

**REST API** (used by the page, also callable directly):

| Endpoint | Purpose |
|---|---|
| `GET /api/plugins` | All loaded plugins with enabled/trust/priority |
| `GET /api/plugins/grouped` | Same, grouped by type |
| `POST /api/plugins/<id>/enable` \| `disable` | Toggle (persists row) |
| `POST /api/plugins/<id>/priority` | `{"priority": N}` or `null` to clear plugin-level override (the default for every capability); non-integer -> 400 |
| `GET /api/plugins/export` | Full config blob; now includes per-plugin `capability_priorities` |
| `POST /api/plugins/import` | Restores repos/install/settings/priorities incl. `capability_priorities` |
| `GET /api/plugins/<id>/capabilities` | Per-capability effective priority + source (`capability`/`plugin`/`manifest`) |
| `POST /api/plugins/<id>/capabilities/<cap>/priority` | `{"priority": N}` or `null` to clear the capability-specific override |
| `GET/POST /api/plugins/<id>/settings` | Per-plugin key/value settings |
| `GET /api/plugins/<id>/health` | failures/last_error/last_run_at |
| `POST /api/plugins/<id>/uninstall` | Remove (Phase 3 marketplace flow) |

**Bundled plugins** ship inside the fnack image at `/app/bundled_plugins/<id>/`
and are auto-installed on startup (trust `official`, enabled by default) — no
user action needed. A user install under `/config/plugins/<id>/` wins over the
bundled copy of the same id.

---

## 7. Versioning rules

- `api_version: "^1.0"` means "I work with fnack plugin API 1.x". fnack will
  refuse to load your plugin if its API major version doesn't match.
- `min_core_version` is the oldest fnack build your plugin needs. fnack
  refuses to load on older cores.
- **Breaking changes on our side** bump the API major version (1.0 → 2.0);
  fnack then shows out-of-range plugins as "needs update" instead of loading
  them.
- If your plugin needs a capability `PluginContext` doesn't have, that's a
  feature request for fnack core — file an issue rather than importing
  `models`/`services` to reach around the boundary.

---

## 8. Publishing to a repository

Repositories let users install your plugin without manual copying. A repo is
just a URL serving a JSON index:

```json
{
  "name": "My Plugin Repo",
  "updated_at": "2026-08-01T00:00:00Z",
  "plugins": [
    {
      "id": "com.example.my-plugin",
      "name": "My Plugin",
      "latest_version": "1.2.0",
      "type": ["event_hook"],
      "description": "...",
      "versions": {
        "1.2.0": {
          "download_url": "https://example.com/releases/my-plugin-1.2.0.zip",
          "sha256": "b2f5...",
          "min_core_version": "0.2.0"
        }
      }
    }
  ]
}
```

To publish:

1. Zip your plugin folder (`plugin.json` + `plugin.py` + any assets) into
   `plugin.zip`. The zip must extract to a folder whose contents include
   `plugin.json` at its root.
2. Host the zip at a stable URL.
3. Compute the SHA-256 of the zip: `sha256sum plugin.zip`.
4. Add an entry to your index JSON with the `download_url` and `sha256`.
5. Serve the index JSON over HTTPS. Users paste the index URL into
   Settings → Plugins → Repositories, then install from the Marketplace.
   fnack verifies the checksum before installing and never runs code from a
   repo without an explicit install action.

---

## 9. What not to do / trust model

- **Do not** import `models`, `app`, or `services.*` from your plugin. You
  only hold a `PluginContext`; reaching for internals breaks the compatibility
  promise and will break on the next fnack update.
- **Declared permissions are enforced.** Using an undeclared capability raises
  `PermissionError`; declared-but-unused permissions are flagged as a warning.
- **Trust tiers** are shown in the UI: Official (fnack-maintained), Verified
  (reviewed by fnack, third-party), Community (everything else — community
  installs get an explicit permission-confirmation dialog).
- fnack v1 runs plugins in-process. A malicious plugin can still do harm
  within its declared permissions (e.g. a `filesystem:downloads` plugin can
  fill your disk). Only install plugins you trust. Real isolation
  (subprocess/container) is the v2 roadmap; because plugins only use
  `PluginContext`, that upgrade doesn't change how you write plugins.
