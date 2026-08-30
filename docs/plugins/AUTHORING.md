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
3. Restart fnack (or in a future release, install via Settings → Plugins).

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
| `library_source` | Source of artists/albums to monitor (planned) | *interface lands with implementation* |
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
| `context.library` | `update_track_status(track_id, status, error_message=None)` | Set a track's status (and optional error). | — |
| `context.library` | `mark_caution(track_id, reason)` | Flag a track for user attention (badge in the UI); does not change status or delete. | — |
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
