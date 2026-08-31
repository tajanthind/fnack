# Phase 7: Core hardcoding audit + Lidarr extraction + 0.2.x-parity trim + scheduled retry

wayfinder:phase-7

## What this phase covers (HARNESS BRIEF 7)

1. **§1 Core-hardcoding audit** — walk every remaining hardcoded call into
   core services and classify it against the core/plugin decision rule:
   1a. Metadata chain re-check (deezer/import discography fetch).
   1b. Lidarr emulation (SABnzbd/Newznab/Torznab routes in app.py).
   1c. navidrome / yt-dlp core calls.
2. **§2 Unified settings modal** — already shipped in Phase 6 (PR #9);
   brief's claim was stale, no work needed.
3. **§3 Trim bundled set to 0.2.x parity** — plugins that did NOT exist in
   0.2.x ship disabled-by-default (opt-in in Marketplace), instead of
   enabled-by-default. 0.2.x-parity posture, not deletion.
4. **§4 Build `fnack.lidarr`** — `library_source` plugin extracting the
   Newznab/SABnzbd emulation from `services/lidarr_service.py`, routes
   behind the plugin (subsonic-style server_extension registration).
5. **§5 Scheduled failed-song retry** — re-queue failed/error tracks on a
   schedule, default daily (user instruction, unlike maintenance's weekly).

## §1a — Metadata chain: FALSE ALARM (verified live)

The brief's "two fresh reads showing old form" are stale GitHub-blob
artifacts again (same failure mode as Brief 5). On local `main` the fix is
intact at `bd2fb2f` (merged via PR #6 → `6f4d043`):

- `app.py` `_sync_artist_discography_background`: per-provider keying
  `key = str(deezer_artist_id) if provider.manifest.id == "fnack.deezer-batch"
  else artist.name` (L472), `served_by` logged (L479), per-provider
  try/except (L488-489).
- `services/import_service.py` has the same priority-iterating shape.
- Ticket `plugin-metadata-chain-live-verification.md` documents the first
  false alarm; this is the same artifact, re-verified.

**Classification: core (rule 2/3), no change needed.**

## §1b — Lidarr: became the `fnack.lidarr` plugin (this phase)

Formerly: `/api/sabnzbd*`, `/api/newznab*`, `/api/torznab*`,
`/api/nzb/<type>/<id>` routes in `app.py` calling `services/lidarr_service.py`
directly. Classification per the decision rule: optional (only useful to
people running Lidarr), swappable, not on any user-facing latency path →
**plugin**. (HARNESS §2's original table said "could be a new
`library_source` plugin type or fit inside `scan_trigger` — ask me which";
user chose `library_source` in Brief 7 §4.)

Delivered:
- `bundled_plugins/fnack.lidarr/` (plugin.json + plugin.py), type
  `["library_source", "server_extension"]`:
  - `register_routes()` registers the same paths (SABnzbd modes, Newznab
    caps/get/search, NZB delivery) via the server_extension blueprint loop.
  - All DB access through `context.library` — no models/services imports.
  - settings_schema covers what lidarr_service read: `api_key` (secret);
    `on_load` mirrors the core M2M key into the plugin setting for display;
    `on_settings_changed` writes a non-empty value through to the core key.
  - Enabled by default: Lidarr existed in 0.2.x (v0.2.32/33 fixed Lidarr
    flows), so 0.2.x parity says enabled — NOT in the `default_disabled` set.
- `plugins/context.py` grew the sanctioned methods the plugin needed:
  `get_or_create_api_key`, `search_albums`, `search_tracks`, `get_album_info`,
  `get_track_info`, `queue_lidarr_grab` (moved verbatim from
  `_create_lidarr_grab_job`), `list_download_jobs`, `cancel_download_job`.
- `services/lidarr_service.py` DELETED — everything extracted.
- app.py: lidarr routes + import removed; core `get_api_key` call sites
  (settings GET + boot) now use `LibraryContext().get_or_create_api_key()`.
- Smoke test: new multi-type fixture (`library_source` + `server_extension`)
  proves the class can implement both interfaces and that `register_routes`
  blueprints actually serve.

## §1c — navidrome / yt-dlp core calls

- navidrome test/scan routes in app.py were calling
  `test_navidrome_connection` / `trigger_navidrome_scan` directly — a
  hardcoded bypass of the `scan_trigger` chain. Now they route through
  `get_scan_triggers()` (the plugin chain), keeping a direct-call fallback
  when no scan_trigger plugin is installed. **Fixed.**
- yt-dlp cookies (`get_cookies_path` / `get_cookies_status` in app.py
  settings GET/POST): classified **core** (rule 1/2) — the download-pipeline
  config surface must work with the yt-dlp plugin disabled, and the plugin
  reads the same path from its own settings (`cookies_path`). Not a bypass:
  the pipeline itself is already plugin-chained (Phase 2).
- Remaining core services (`queue_service`, `verifier`, `metadata_service`,
  `watcher_service`) are core by rule 2/3 (queue engine, verifier is the
  zero-mismatch guarantee, folder watcher is basic library import).

## §3 — 0.2.x-parity default posture

`default_disabled` set added to the bundled auto-install gate in app.py:

| plugin | why disabled-by-default |
|---|---|
| `fnack.subsonic` | Subsonic SERVER API — did not exist in 0.2.x |
| `fnack.discord-webhook` | Phase 4 webhook pack — did not exist in 0.2.x |
| `fnack.ntfy-webhook` | Phase 4 webhook pack — did not exist in 0.2.x |
| `fnack.reverse-proxy-auth` | Phase 4 auth (also already gated as auth_provider) |

Still listed in Marketplace (opt-in). Everything that existed in 0.2.x stays
enabled-by-default (navidrome, ytdlp, spotiflac, deezer-batch, ..., lidarr).

## §5 — Scheduled failed-song retry (default daily)

- `_retry_all_failed()` extracted from the manual "Retry All Failed" route —
  one implementation, two triggers (manual button + scheduler).
- `_periodic_failed_retry_loop()` background task: checks every 5 min
  whether `_retry_interval_seconds()` has elapsed since `_last_retry`;
  re-queues all failed/error tracks + jobs.
- New `retry_interval` AppSetting (default `"daily"`, per user instruction —
  the brief explicitly overrode maintenance's weekly default): `weekly`,
  `daily`, `restart`, `manual` — same interval map as maintenance.
- settings.html: Failed-Song Auto-Retry Schedule dropdown + "Retry All
  Failed Now" button; setting wired into load/dirty/payload/reload.

## Files

- `app.py` (lidarr routes/import removed; navidrome test/scan → chain;
  default_disabled set; retry scheduler + `retry_interval`; api_key via
  LibraryContext)
- `services/lidarr_service.py` (deleted)
- `plugins/context.py` (new library methods)
- `bundled_plugins/fnack.lidarr/` (new plugin)
- `templates/settings.html` (retry UI + lidarr note)
- `docs/plugins/AUTHORING.md` (context table + library_source row)
- `tests/run_smoke_test.py` (multi-type fixture + route registration)
- fnack-plugins repo: `plugins/fnack.lidarr/` added, index repackaged

## Verification

- `tests/run_smoke_test.py` PASSED (incl. new lidarr fixture).
- `py_compile` clean on all touched modules.
- PR #10 opened on `plugin-architecture/phase-7-core-trim-and-hardcoding` —
  merged by user, then container redeployed from main for live E2E.

## Blocked by

(none)
