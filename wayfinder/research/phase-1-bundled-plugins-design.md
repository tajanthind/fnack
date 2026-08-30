# Phase 1 bundled plugins — design findings

Status: research (no code changed). Companion ticket:
`wayfinder/tickets/plugin-phase-1-design.md`. Branch: `plugin-architecture/phase-0-scaffold`
(Phase 1 implementation starts after the user merges Phase 0, per HARNESS §0).

Scope: concrete manifest inventory + plugin class skeletons + auto-install
flow + Settings → Plugins UI + behavior-preservation callouts, so Phase 1
implementation is mechanical.

---

## 1. Bundled plugin inventory

Physical home in the image: **`/app/bundled_plugins/<id>/`**, baked by a
Dockerfile `COPY bundled_plugins /app/bundled_plugins`. The manager discovers
both `/app/bundled_plugins` (bundled, official) and `/config/plugins`
(user/third-party) — see §2.

All bundled plugins: `"trust_level": "official"`, `"api_version": "^1.0"`,
`"min_core_version": "0.2.0"`, `"author": "fnack"`.

### 1.1 `fnack.spotiflac` — downloader, priority 10 (primary)

```json
{
  "id": "fnack.spotiflac",
  "name": "SpotiFLAC",
  "version": "1.0.0",
  "type": ["downloader"],
  "api_version": "^1.0",
  "min_core_version": "0.2.0",
  "entry_point": "plugin:SpotiFLACDownloader",
  "author": "fnack",
  "description": "Lossless audio via SpotiFLAC (Tidal/Qobuz/Deezer/SoundCloud, zero-auth). Tried first.",
  "permissions": ["network", "filesystem:downloads"],
  "settings_schema": [
    {"key": "quality", "type": "select", "options": ["LOSSLESS", "HIGH"], "default": "LOSSLESS"},
    {"key": "delay", "type": "number", "default": "3.0"}
  ]
}
```

```python
from plugins.base import DownloaderPlugin, DownloadResult, TrackRef
from services.spotiflac_service import (  # NOTE: bundled plugins MAY import services/* (first-party); third-party plugins may not
    download_track_spotiflac, is_spotiflac_rate_limited,
)


class SpotiFLACDownloader(DownloaderPlugin):
    priority = 10

    def can_handle(self, track: TrackRef) -> bool:
        return bool(track.spotify_url)

    def download(self, track, dest_dir, options) -> DownloadResult:
        ok, file, err = download_track_spotiflac(
            track.spotify_url, dest_dir,
            quality=options.get("quality", "LOSSLESS"),
            delay=options.get("delay", 3.0),
        )
        return DownloadResult(success=ok, file_path=file, error=err,
                              source_plugin_id=self.manifest.id,
                              extra={"format": file.suffix.lstrip(".") if file else None})

    def is_rate_limited(self) -> bool:
        return is_spotiflac_rate_limited()
```

State that must move into the instance: the module-level 429 circuit breaker
(`_rate_limited_until`, `_on_rate_limit_detected`, `_on_success`) and the
pacing delay — currently module globals in `spotiflac_service.py`; keep the
module globals (they're process-wide) but route all calls through the plugin
instance so disabling the plugin stops the breaker from being consulted.

### 1.2 `fnack.ytdlp` — downloader, priority 50 (fallback); **spotdl is an alias**

```json
{
  "id": "fnack.ytdlp",
  "name": "yt-dlp",
  "version": "1.0.0",
  "type": ["downloader"],
  "api_version": "^1.0",
  "min_core_version": "0.2.0",
  "entry_point": "plugin:YtDlpDownloader",
  "author": "fnack",
  "description": "YouTube/YouTube Music fallback downloader (also serves the legacy spotdl entry point).",
  "permissions": ["network", "filesystem:downloads"],
  "settings_schema": [
    {"key": "format", "type": "select", "options": ["opus", "flac", "mp3"], "default": "opus"},
    {"key": "audio_source", "type": "select", "options": ["youtube", "youtube_music"], "default": "youtube_music"},
    {"key": "cookies_path", "type": "string", "default": "/config/cookies.txt"}
  ]
}
```

- `can_handle`: True for any track (yt-dlp is the universal fallback); skip when
  `is_rate_limited()` or upstream circuit open.
- `download` calls `services.ytdlp_service.download_track_ytdlp(...)` 1:1.
- **spotdl**: `services/spotdl_service.py` is a thin shim whose only function
  forwards 1:1 to `download_track_ytdlp`. Per the user decision, spotdl is an
  **alias of this plugin** — `can_handle`/`download` also accept the spotdl
  call shape (`audio_source="youtube"` default, format flac default). No
  separate `fnack.spotdl` manifest row. After migration, grep for remaining
  `spotdl_service` imports (queue_service imports it today); if dead, flag
  deletion (don't silently remove).

### 1.3 `fnack.deezer-batch` — metadata_provider, priority 10 (batch path only)

```json
{
  "id": "fnack.deezer-batch",
  "name": "Deezer (batch enrichment)",
  "version": "1.0.0",
  "type": ["metadata_provider"],
  "api_version": "^1.0",
  "min_core_version": "0.2.0",
  "entry_point": "plugin:DeezerBatchProvider",
  "author": "fnack",
  "description": "Deezer discography/metadata enrichment for sync & import. Authoritative. The interactive /api/search-artist route stays CORE.",
  "permissions": ["network"]
}
```

- `search_artist(name)` → `deezer_service.search_artist(name, limit=10)`.
- `get_artist_discography(provider_artist_id)` → `deezer_service.get_artist_discography(int(provider_artist_id))`.
- `get_track_info(provider_track_id)` → `deezer_service.get_track_info(int(provider_track_id))`.
- The **interactive route is NOT migrated** — `app.py:/api/search-artist`
  keeps calling `deezer_service.search_artist` directly (core, user-confirmed).

### 1.4 `fnack.musicbrainz` — metadata_provider, priority 20 (enrichment-only)

```json
{
  "id": "fnack.musicbrainz",
  "name": "MusicBrainz",
  "version": "1.0.0",
  "type": ["metadata_provider"],
  "api_version": "^1.0",
  "min_core_version": "0.2.0",
  "entry_point": "plugin:MusicBrainzProvider",
  "author": "fnack",
  "description": "Enrichment-only MusicBrainz lookup (adds mb_* facts; never removes/renames). 1 req/s + Retry-After preserved.",
  "permissions": ["network"]
}
```

- `search_artist(name)` → `musicbrainz_service.search_artist_cached(name)`.
- `get_artist_discography(mb_artist_id)` → raise/return None: MusicBrainz has no
  full discography method; enrichment is `enrich_albums` (mutates Deezer
  albums), which stays core glue called from sync/import orchestration. The
  provider exposes `search_artist` only; `enrich_albums` stays a core function
  that internally uses the pacing + cache.
- **Rate limit preservation**: `MIN_INTERVAL = 1.0` pacing + Retry-After
  backoff + negative cache (30d) are module state in `musicbrainz_service.py`.
  The plugin wraps the same module functions — the pacing stays inside
  `musicbrainz_service` (called through the plugin), so it is preserved
  verbatim, NOT "one more provider in the list".

### 1.5 `fnack.spotify` — metadata_provider, priority 30

- Wraps `spotify_service.resolve_spotify_url(...)` (ISRC → Spotify URL for the
  download pipeline) and `search_artist`-style lookups where used.
- `get_track_info`/`search_artist` map to the existing ISRC resolution helpers.
- It is NOT user-facing search (the search box is Deezer); it's a pipeline
  resolver → plain metadata_provider plugin.

### 1.6 `fnack.itunes` — metadata_provider, priority 40 (fallback)

- `search_artist(name)` → `get_itunes_artist_albums(artist_query, ...)`.
- `get_track_info(provider_id)` → `get_itunes_album_tracks(collection_id)`.
- No settings.

### 1.7 `fnack.acoustid` — fingerprint plugin (safety-relevant behavior preserved exactly)

```json
{
  "id": "fnack.acoustid",
  "name": "AcoustID Fingerprinting",
  "version": "1.0.0",
  "type": ["fingerprint"],
  "api_version": "^1.0",
  "min_core_version": "0.2.0",
  "entry_point": "plugin:AcoustIDFingerprinter",
  "author": "fnack",
  "description": "Optional acoustic verification: confirms a download is the right song, identifies unknown/regional tracks, flags mismatches. Keyless = disabled.",
  "permissions": ["network", "filesystem:music"],
  "settings_schema": [
    {"key": "api_key", "type": "secret", "required": false}
  ]
}
```

- `identify(file_path)` → `acoustid_service.identify(str(file_path))`, mapping
  to `FingerprintResult(confidence, matched_title, matched_artist, raw)`.
- **Preserve exactly** (from wayfinder tickets acoustid-fingerprinting.md +
  regional-artist-fallback.md): verify-when-unsure at the 0.8 gate; keyless →
  disabled (silent no-op, no match attempts); regional no-match changes
  nothing; confirmed mismatch → caution flag with "what it matched to" and
  user keep/delete. The `verify_download` + `_last_lookup_*` flags and the
  caution flagging path stay intact — the plugin exposes `identify`, and core
  (queue `_verify_or_rescue`) keeps calling the underlying verify logic, which
  now routes through the fingerprint plugin chain (single plugin, so 1:1).

### 1.8 `fnack.navidrome` — scan_trigger + settings_tab

```json
{
  "id": "fnack.navidrome",
  "name": "Navidrome",
  "version": "1.0.0",
  "type": ["scan_trigger", "ui_extension"],
  "api_version": "^1.0",
  "min_core_version": "0.2.0",
  "entry_point": "plugin:NavidromePlugin",
  "author": "fnack",
  "description": "Triggers Navidrome rescans and contributes the Navidrome settings panel.",
  "permissions": ["network", "settings"],
  "ui": {"slots": ["settings_tab"]},
  "settings_schema": [
    {"key": "url", "type": "string", "default": ""},
    {"key": "user", "type": "string", "default": ""},
    {"key": "token", "type": "secret", "default": ""},
    {"key": "auto_scan", "type": "boolean", "default": "true"},
    {"key": "db_path", "type": "string", "default": ""}
  ]
}
```

- `trigger_scan()` → `navidrome_service.trigger_navidrome_scan(app)` — NOTE:
  this takes `app` today; the plugin has no `app`. Either `PluginContext`
  gains a `context.app`-free trigger (the scan trigger reads settings from
  `context.settings` instead of AppSetting rows — behavior-preserving since
  the settings move into the plugin's namespaced store with the same keys),
  or `context.jobs`/`context.library` expose what's needed. **Proposed
  PluginContext addition (HARNESS §5.5 — needs a nod): nothing app-shaped;
  the plugin reads url/user/token/auto_scan/db_path from context.settings.**
- `test_connection()` → `navidrome_service.test_navidrome_connection(...)`.
- The Navidrome settings fields currently in the main settings page move into
  the plugin's `settings_tab` slot; the `/api/navidrome/*` routes in app.py
  become thin wrappers over the plugin (or stay core calling the plugin).

### 1.9 `fnack.vpn` — vpn plugin (includes the http_proxy fold-in)

```json
{
  "id": "fnack.vpn",
  "name": "VPN (WireGuard/OpenVPN)",
  "version": "1.0.0",
  "type": ["vpn"],
  "api_version": "^1.0",
  "min_core_version": "0.2.0",
  "entry_point": "plugin:VPNPluginImpl",
  "author": "fnack",
  "description": "Split-mode WireGuard/OpenVPN tunnel for download/metadata traffic.",
  "permissions": ["settings", "filesystem:downloads"]
}
```

- `start()`/`stop()`/`status()` → `vpn_service.start_vpn`/`stop_vpn`/
  `get_vpn_status` equivalents (confirm exact names in vpn_service.py).
- **`scripts/http_proxy.py`** is the split-mode HTTP CONNECT proxy (spawned by
  the vpn machinery, uid 2001) — it folds INTO this plugin's implementation
  (spawned via subprocess, same as today), it is NOT a `library_task`.

### 1.10 Four `library_task` plugins

- `fnack.clean-navidrome-artists` → `scripts/clean_navidrome_artists.py`
  (delete non-artist roles + empty artist rows).
- `fnack.normalize-album-tags` → `scripts/normalize_album_tags.py`.
- `fnack.reverify-library` → `scripts/reverify_library.py`.
- `fnack.fix-navidrome-splits` → `scripts/fix_navidrome_splits.py`.

Each:

```python
from plugins.base import LibraryTaskPlugin, TaskResult


class CleanNavidromeArtists(LibraryTaskPlugin):
    schedule = "manual"  # or None

    def run(self) -> TaskResult:
        from scripts.clean_navidrome_artists import main
        result = main()
        return TaskResult(success=True, message=str(result))
```

- `scripts/run_maintenance.py` KEEPS working as a CLI that calls the same
  underlying functions (cron/headless compat); its orchestration role is
  superseded by the Maintenance panel calling `plugin_manager.get_library_tasks()`
  → `call_safe(task, "run")`.

### 1.11 Stays core (not plugins) — per user decisions

- `queue_service.py` engine, `models.py`, auth/API-key, DB layer
- Plugin manager / event bus / marketplace UI / `plugins/*` core machinery
- `import_service.py` + `metadata_service.py` — orchestrators that CALL the
  chains (core glue), not plugins
- `verifier_service.py` — safety-critical, always on, never disable-able
- Interactive `/api/search-artist` (Deezer direct), caching layers
- `lidarr_service.py` → designed as `library_source` type but NOT migrated in
  Phase 1 (deferred); `watcher_service.py` → event-driven, stays core for now

---

## 2. Auto-install on fresh/existing DBs (PHASE1 §3)

Startup (in app.py's boot `app_context`, replacing the current
`plugin_manager.load_all(enabled_ids=...)`):

1. `manager.plugins_dir` = `/config/plugins` (user dir); add
   `manager.bundled_plugins_dir` = `/app/bundled_plugins` (image-baked).
   `discover()` scans BOTH dirs; `load_plugin()` is unchanged (works on any
   dir with plugin.json).
2. Before `load_all`: for every bundled plugin dir discovered under
   `/app/bundled_plugins`, if no `InstalledPlugin` row exists for its
   manifest id, create one: `trust_level="official"`, `enabled=True`,
   `source_repo_id=None`, version from manifest. Idempotent (only inserts
   missing rows) — existing rows untouched (a user who disabled a bundled
   plugin stays disabled).
3. Then `enabled_ids = {p.id for p in InstalledPlugin.query.filter_by(enabled=True)}`
   and `load_all(enabled_ids)` as today. Result: bundled plugins are
   enabled-by-default with zero user action — no marketplace visit needed.

---

## 3. Settings → Plugins UI (PHASE1 §4)

User chose: **new top-level tab/page** (not a settings.html card).

- New route `GET /plugins` → `templates/plugins.html`, following
  `settings.html` conventions (same navbar, theme, `app.js` include).
  Nav link "Plugins" added to the top navbar + mobile bottom nav in all
  templates (index/import/queue/settings).
- Backend: reuse the existing `/api/plugins` blueprint (list/enable/disable/
  settings/health) — already implemented in Phase 0. Add:
  - `POST /api/plugins/<id>/priority` `{priority: int}` → writes
    `InstalledPlugin.priority_override` (see below) and persists.
  - Grouped-by-type GET (`/api/plugins` already returns `type` lists — group
    client-side or add `?grouped=1`).
- List view grouped by type (Downloaders, Metadata Providers, Fingerprinting,
  Scan Triggers, Library Tasks, VPN, Event Hooks, Server Extensions, UI
  Extensions): each row shows name, version, trust badge (Official/Verified/
  Community), enabled toggle, health (consecutive_failures, last_error,
  last_run_at — from the `/api/plugins/<id>/health` endpoint).
- Enable/disable toggle → `POST /api/plugins/<id>/enable|disable` (already
  wired: calls `on_enable()`/`on_disable()` + persists `InstalledPlugin.enabled`).
- **Priority**: add nullable `priority_override` INTEGER column on
  `InstalledPlugin` (via the ALTER-TABLE migration pattern already used in
  app.py's boot block — matches how mb_* and caution columns were added; a
  PluginSetting key would work but a column is consistent with per-install
  state living on the row). `PluginManager.get_downloaders()` /
  `get_metadata_providers()` sort by `priority_override` when set, else
  manifest `priority`. UI: **numeric input** per plugin row (user-confirmed),
  writes via the priority endpoint on save.
- Marketplace/Repositories tabs: stub pages (empty states) that Phase 3 fills —
  enable/disable + priority work identically for bundled and third-party from
  day one.

---

## 4. Write-lock discipline (from scale-to-millions research)

- Scaffold `call_safe` is already commit-free (good — keep it).
- Enable/disable/priority/settings endpoints do their OWN single commit (they
  are user-actioned, low-frequency — acceptable).
- The plugin **health-log** (consecutive_failures/last_error/last_run_at
  updates from `call_safe`) must NOT commit per hook call. Buffer in-memory;
  flush piggybacked on queue_service's existing commit points (854/900/924/952
  in `_process_track_job`) or a periodic flush (≤5s). Implement in Phase 1.

---

## 5. Behavior-preservation callouts (PHASE1 §6.4)

1. **Bundled must be enabled-by-default** — if auto-install missed a plugin,
   downloads break (no downloader chain) or metadata enrichment silently stops.
   Guard: smoke test asserts all bundled plugin ids have InstalledPlugin rows.
2. **AcoustID behavior** — the 0.8 verify gate, silent regional no-match, and
   caution-flag path are safety-tuned; the plugin must expose `identify` while
   core keeps `verify_download` semantics identical (including
   `_last_lookup_had_results`/`_last_lookup_missing_metadata` flags used by
   the Identify endpoint).
3. **MusicBrainz pacing** — 1 req/s + Retry-After + negative cache must not be
   lost when the provider moves behind the plugin chain (single provider for
   now → same behavior; a future second provider must inherit the pacing).
4. **Settings keys** — moving Navidrome/VPN/AcoustID settings from `AppSetting`
   rows into `PluginSetting` must keep the same keys/defaults so existing
   user config carries over (migration copies values on first boot).
5. **Rate-limit state** — SpotiFLAC's 429 circuit breaker + pacing delay are
   module globals; moving calls behind the plugin must keep them process-wide
   (a disabled plugin skips its breaker, but an enabled plugin consults the
   same state as before).
6. **Error messages / timing** — the queue's failure_reasons strings come from
   the services; the plugin wrapper must pass them through unchanged so the
   UI/queue history shows identical text.
7. **`run_maintenance.py` CLI** — must keep working (cron/headless); it calls
   the same functions the library_task plugins wrap, not deleted.
8. **`/api/navidrome/*`, `/api/vpn/*`, `/api/maintenance/run` routes** — keep
   returning identical JSON; they become thin wrappers over the plugins, not
   new shapes.

---

### Live finding (validated on the real container, this run)

**Manual-install enable does not persist across restart.** The scaffold's
`POST /api/plugins/<id>/enable` / `disable` endpoints call
`manager.enable_plugin(...)` (in-memory) and then only
`row.enabled = ...` **if an `InstalledPlugin` row already exists**
(`db.session.get(InstalledPlugin, plugin_id)` then `if row:`). A manual
folder install (`/config/plugins/<id>/`) has NO row, so enabling it works
for the current process but reverts on restart. Verified live: install
example plugin → enable → `enabled: True` → restart → `enabled: False`.

Implications for Phase 1:
1. **Bundled auto-install (§2) closes this for bundled plugins** — rows are
   created at startup, so bundled enable/disable persists. Good.
2. **For manual/third-party installs**, the enable endpoint (and the Phase 1
   Settings→Plugins toggle) must CREATE the `InstalledPlugin` row when
   missing (`enabled=True`, trust from manifest, source_repo_id=None) rather
   than silently no-op'ing. This is a small scaffold change to make during
   Phase 1 (in `plugins/api.py` enable/disable + the UI toggle path).
3. `/api/plugins/<id>/health` returns `{"error": "not installed"}` for
   row-less manual installs — after (2), rows always exist so health works.

## 6. Summary (priority order for Phase 1 implementation)

1. Manager: `bundled_plugins_dir` discovery + auto-install rows (§2) — the
   regression guard for everything else.
2. `InstalledPlugin.priority_override` column + ordered-getter sort + numeric
   priority endpoint (§3).
3. Downloader plugins (spotiflac p10, ytdlp p50 + spotdl alias) + queue
   pipeline call-site swap (Phase 2's loop can come later; Phase 1 keeps the
   hardcoded sequence calling the plugin chain 1:1).
4. Metadata providers (deezer-batch p10, musicbrainz p20 w/ pacing, spotify
   p30, itunes p40).
5. Acoustid fingerprint + navidrome scan_trigger + vpn plugins (behavior-
   preserving wraps; move settings into PluginSetting with value migration).
6. Four library_task plugins + Maintenance panel calls + run_maintenance CLI
   compatibility.
7. Settings → Plugins page (grouped list, enable/disable, numeric priority,
   health, stubbed marketplace/repos).
8. Health-log buffering with piggybacked flush (§4).
