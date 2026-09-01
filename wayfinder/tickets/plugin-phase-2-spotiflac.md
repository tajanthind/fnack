# Phase 2 (PR 3): DownloadService + SpotiFLAC extraction

wayfinder:phase-2

## What this covers (02-PHASE-2-EXTRACT-OFFICIAL-PROVIDERS.md §1 + handoff PR 3)

First provider extraction. fnack.spotiflac becomes the AUTHORITATIVE
implementation — no longer a wrapper around a core service.

## What was done

1. **Provider implementation moved into the plugin**
   `services/spotiflac_service.py` (353 lines) moved verbatim to
   `bundled_plugins/fnack.spotiflac/spotiflac.py`: CLI process invocation,
   Xvfb handling, extension management (`ensure_spotiflac_extensions`,
   SoundCloud patch), retries, pacing rate limiter, and the 429 circuit
   breaker — all provider state now lives in the plugin.
2. **Plugin implements the FINAL SDK contract** — `SpotiFLACPlugin(
   PluginBase, TrackDownloader)` with `async can_handle(request:
   DownloadRequest)` / `async download(request: DownloadRequest) ->
   DownloadResult` (provider_id/success/path/error_code/message/retryable/
   metadata). `is_rate_limited()` stays for the queue chain's generic
   circuit-breaker check. Entry point renamed to `SpotiFLACPlugin`;
   manifest gains `timeout` setting (plugin-owned quality/delay/timeout).
3. **Multi-file plugin support** — the manager now puts the plugin dir on
   `sys.path` during import (like deps), so `import spotiflac` resolves.
4. **DownloadService migration adapter** (`services/queue_service.py`) —
   `_is_sdk_downloader` / `_invoke_downloader_can_handle` /
   `_invoke_downloader_download`: new-contract providers get a
   `DownloadRequest`, legacy providers keep old args; the SDK
   `DownloadResult` is normalized to the legacy `success/file_path/error`
   shape so downstream verification is untouched. The chain iterates
   `get_downloaders()` which now returns capability-registry providers
   (SDK or legacy).
5. **Manual-download path** (`download_manual_match_track`) — the 3 direct
   `download_track_spotiflac(...)` calls now go through
   `_download_via_spotiflac_provider(...)` (guarded manager boundary).
6. **vpn_service decoupled** — `reset_spotiflac_rate_limit` import replaced
   with emitting `network.route_changed`; the plugin subscribes in `on_load`
   and resets its own breaker (provider state stays in the plugin).
7. **`services/spotiflac_service.py` DELETED**; `download_track_spotiflac`
   / `is_spotiflac_rate_limited` no longer importable from core.
8. **Legacy settings migration** — plugin settings authoritative
   (quality/delay/timeout); legacy `spotiflac_quality`/`spotiflac_delay`
   globals are a one-time fallback migrated into the plugin store in
   `on_load`. Full legacy-setting/UI deletion deferred to PR 11/12 (handoff).
9. **fnack-plugins sync** — plugin.py/plugin.json/spotiflac.py byte-identical
   in both repos; index repackaged (zip now contains all 3 files).
10. **Parity + architecture tests** —
    `tests/architecture/test_spotiflac_extraction.py` (impl in plugin not
    core; SDK contract shape; adapter normalizes both contracts; manual path
    routes through provider; vpn emits event). Independence-test allowlist
    shrank: spotiflac entries removed.

## Decision notes

- The plugin subclasses `PluginBase` AND implements the `TrackDownloader`
  protocol — the manager requires a PluginBase subclass to load; the
  protocol gives the FINAL contract. `get_downloaders()` prefers the
  capability registry (returns both SDK and legacy providers).
- `engine_gates` legacy enable toggles (`enable_spotiflac` etc.) remain
  temporarily (documented in the independence test as the transitional
  fence); PR 11/12 removes them with the settings migration.
- Queue behavior is preserved: same priority order (spotiflac p10 < ytdlp
  p50), same per-engine verification, same failure surface. The adapter
  normalizes result shapes so the verification code never changes.

## Files

- `bundled_plugins/fnack.spotiflac/spotiflac.py` (moved impl, new)
- `bundled_plugins/fnack.spotiflac/plugin.py` (SDK contract + settings +
  route-changed subscription)
- `bundled_plugins/fnack.spotiflac/plugin.json` (entry point + timeout)
- `services/spotiflac_service.py` (deleted)
- `services/queue_service.py` (migration adapter + manual path + get_downloaders)
- `services/vpn_service.py` (event emission)
- `plugins/manager.py` (multi-file plugin import; get_downloaders via registry)
- `plugins/events.py` (network.route_changed in KNOWN_EVENTS)
- `fnack/plugin_api/models.py` (FINAL SDK DownloadResult shape)
- `tests/architecture/test_spotiflac_extraction.py` (new parity test)
- `tests/architecture/test_core_provider_independence.py` + `test_plugin_boundary.py`
  (spotiflac removed from transitional allowlists)
- `docs/plugins/AUTHORING.md`
- fnack-plugins: plugin synced + repackaged (3 files in zip)

## Verification

- Smoke test PASSED; all 6 architecture tests PASSED (incl. new
  spotiflac extraction parity test).
- Live boot: spotiflac loads as an SDK TrackDownloader, registered for
  download.track, priority-ordered first; adapter end-to-end (can_handle +
  download via DownloadRequest → legacy-shaped result); route_changed
  subscription verified; zero-auth preserved.

## Blocked by

(none)
