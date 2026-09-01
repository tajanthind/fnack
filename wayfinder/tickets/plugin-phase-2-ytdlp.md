# Phase 2 (PR 4): yt-dlp provider extraction

wayfinder:phase-2

## What this covers (02-PHASE-2-EXTRACT-OFFICIAL-PROVIDERS.md §1 + handoff PR 4)

Second provider extraction. fnack.ytdlp becomes the AUTHORITATIVE
implementation — the engine moves out of core into the plugin and the
plugin implements the FINAL SDK `TrackDownloader` contract (coexisting with
the still-legacy providers until their own PRs).

## What was done

1. **Engine moved into the plugin**
   `services/ytdlp_service.py` (644 lines) moved verbatim to
   `bundled_plugins/fnack.ytdlp/ytdlp.py`: yt-dlp CLI invocation, candidate
   scoring, YouTube Music preference, cookies handling (per-plugin copy so
   yt-dlp's cookie-jar dump never overwrites the user's upload), format
   selection, and yt-dlp-specific error parsing. The legacy
   `services/spotdl_service.py` alias is deleted too — spotdl is now
   migration metadata only (ticket plugin-spotdl-form.md).
2. **Plugin implements the FINAL SDK contract** — `YtDlpDownloader(
   PluginBase, TrackDownloader)` with `async can_handle(request:
   DownloadRequest)` (any track or raw query/URL) and `async download(
   request: DownloadRequest) -> DownloadResult`. `download()` reads the
   provider-neutral hints off the request (format / audio_source /
   cookies_path / check_duration / query) and falls back to its own plugin
   settings; query or `"{artist} - {title}"`. Generic core helpers
   (`verify_audio_file`, `verify_download` AcoustID) are injected through
   the PluginContext facade — the plugin never imports services.*.
3. **DownloadRequest extended with provider-neutral hints** (PR 4) —
   `query`, `cookies_path`, `audio_source`, `check_duration` fields (the
   queue's post-download verification stays in core, so `check_duration`
   lets the provider skip its own internal check).
4. **Migration adapter extended** (`services/queue_service.py`) —
   `_build_download_request` now carries the hints; the chain still
   normalizes the SDK `DownloadResult` to the legacy
   `success/file_path/error` shape so downstream verification is untouched.
   Engine gates reduced to `{"fnack.ytdlp": enable_ytdlp,
   "fnack.spotdl": enable_ytdlp}` — the spotiflac gate is gone (its
   extraction PR removed it); `enable_ytdlp` itself stays until the
   settings-migration PR (PR 11/12) per the reviewer's table.
5. **Manual-download path** — the 5 remaining `download_track_ytdlp(...)`
   calls in `download_manual_match_track` now route through
   `_download_via_ytdlp_provider(...)` (guarded manager boundary over the
   download.track capability). Spotify-URL fallback logic untouched.
6. **Cookies settings UI routed through the provider** — `app.py` no longer
   imports `get_cookies_path`/`get_cookies_status` from the deleted service;
   the routes use `_cookies_provider()` (duck-typed: any enabled
   download.track provider exposing `get_cookies_status`), with minimal core
   fallbacks `_cookies_status()`/`_cookies_path()`.
7. **`services/ytdlp_service.py` + `services/spotdl_service.py` DELETED** —
   `download_track_ytdlp` no longer importable from core.
8. **Legacy settings migration** — plugin settings authoritative
   (format/audio_source/cookies_path/timeout); legacy
   `ytdlp_format`/`spotdl_format`/`spotdl_source`/`youtube_cookies_path`
   globals are a one-time fallback migrated in `on_load`. Full
   legacy-setting/UI deletion deferred to PR 11/12 (handoff).
9. **fnack-plugins sync** — plugin.py/plugin.json/ytdlp.py byte-identical
   in both repos; index repackaged (zip now contains all 3 files).
10. **Parity + architecture tests** —
    `tests/architecture/test_ytdlp_extraction.py` (impl in plugin not core
    + services deleted; SDK contract + cookies helpers; adapter passes hints
    + normalizes; manual path routes through provider; cookies UI routes
    through provider — no services import in app.py). Independence-test and
    boundary-test allowlists updated (ytdlp/spotdl removed from forbidden +
    transitional; ytdlp leaves the boundary transitional list; plugin may
    call verifier/acoustid core helpers via the context facade).

## Decision notes

- The plugin subclasses `PluginBase` AND implements the `TrackDownloader`
  protocol (same pattern as fnack.spotiflac). The queue chain's
  `get_downloaders()` returns SDK + legacy providers via the capability
  registry; the adapter picks the calling convention per provider.
- `download()` coerces `request.destination` with `Path(...)` — the SDK
  contract types it as Path, but string paths from any caller must not
  crash the plugin.
- `enable_ytdlp` engine gate deliberately NOT removed here: reviewer's
  table (PR #16 review) says keep until the settings-migration PR; the
  gate is now purely cosmetic (both engine entries map to the same SDK
  provider) and is cleaned up with legacy settings in PR 11/12.
