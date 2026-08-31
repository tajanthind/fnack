# Phase 1.1 — Remaining architecture fixes before provider extraction

wayfinder:phase-1.1

## What this phase covers (FNACK Phase 1.1 brief)

Small, focused corrections after Phase 1 (PR #12) before Phase 2 provider
extraction. NO provider moves, NO legacy-service deletions, NO queue behavior
changes, NO ShazamIO, no second abstraction layer.

1. **Capability-specific priority** — `(plugin_id, capability_id) -> priority`
   instead of plugin-wide priority.
2. **Capability declaration validation** — a manifest capability the plugin
   does not implement is skipped (not the whole plugin) with a clear warning.
3. **ProviderExecutor is the runtime invocation boundary** — app-service
   capability/provider calls go through it (sync/async/awaitable/timeout/
   error normalization), not raw method calls.
4. **SDK boundary review** — documented re-export debt.
5. **Tightened architecture tests** (Tests A–F).

## §1 — Capability-specific priority

- **Data model**: `PluginCapabilityPriority(plugin_id, capability_id,
  priority)` table in plugins/models.py — same SQLite/`db` as everything else
  (no second storage system). Created by `db.create_all()` at boot for fresh
  AND existing DBs.
- **Resolution chain (LOWEST number = tried first — preserved from fnack's
  existing downloader/metadata semantics, documented)**:
  `capability-specific override > plugin-level priority_override > manifest
  priority`. A row exists only when the user set a capability-specific value;
  absence = use plugin-level default — so **existing plugin-level priorities
  are preserved without any migration rewrite** (verified live: plugin
  override 7 → default for all caps; cap override 2 wins; clear → back to 7).
- **Registry**: `ProviderHandle` gains `priorities: Mapping[str, int]`;
  `providers(capability)` and the new `providers_for(capability)` sort by the
  EFFECTIVE per-capability priority; deterministic tie-break `(priority,
  plugin_id)` — never installation/insertion order (Test: ties broken by id).
- **Manager**: `LoadedPlugin.capability_priorities` loaded via
  `refresh_from_db`; `_effective_priority(loaded, capability=None)`;
  `set_capability_priority` / `get_capability_priorities` (DB-tolerant
  in-memory fallback so architecture tests run without a Flask app).
- **API**: `GET /api/plugins/<id>/capabilities` (effective priority + source
  `capability`/`plugin`/`manifest`); `POST
  /api/plugins/<id>/capabilities/<cap>/priority` (`{"priority": N}` or
  `null` to clear). Unknown capability / priority < 1 → 400.
- **UI**: no redesign (per brief) — plugin-level Priority input remains the
  default; per-capability config is API-only. UI follow-up documented here
  and in docs/plugins/AUTHORING.md.
- `list_loaded` now exposes `capability_priorities` per plugin.

Live verification (main machine): `track.metadata` = deezer 5 → itunes 30;
`artist.search` = musicbrainz 20 → deezer 25 → itunes 40 — one plugin
(fnack.deezer-batch) at DIFFERENT priorities per capability. Downloader and
metadata chains remain priority-functional (spotiflac 10 < ytdlp 50, etc.).

## §2 — Capability declaration validation

- **Single contract mapping**: `fnack/plugin_api/contracts.py` —
  `CAPABILITY_METHODS` (capability → required method names) +
  `CAPABILITY_BASE_CLASS` (e.g. notification.event → EventHookPlugin) +
  `validate_capability_contract()`. This is the ONLY copy of the mapping
  (manager imports it; do not duplicate).
- **Method names = actual FNACK interfaces**: download.track →
  can_handle/download; track.resolve → resolve_track_url; track.metadata →
  get_track_info; artist.search → search_artist; artist.discography →
  get_artist_discography; album.metadata → get_album_info; fingerprint →
  identify; media.scan → trigger_scan; media.connection_test →
  test_connection; library.task → run; server.extension → register_routes;
  auth.provider → authenticate; network.route → start/stop/status.
- **Behavior**: `_resolve_capabilities` validates each candidate; invalid
  caps are SKIPPED with a warning containing plugin id, capability id, and
  the missing method(s) — valid caps from the same plugin still load (no
  all-or-nothing). No cryptic AttributeError at invocation time.
- **Official manifests corrected** (data only — they declared capabilities
  their plugin objects don't implement): deezer-batch and musicbrainz and
  itunes dropped `album.metadata`; musicbrainz dropped `track.metadata`;
  navidrome dropped `media.health`. Synced to fnack-plugins (pushed
  `45d613c`, index repackaged).

## §3 — ProviderExecutor is the invocation boundary

- New `PluginManager.invoke_provider(loaded, method_name, *args, timeout,
  **kwargs)`: routes the actual provider call through
  `self.executor.run(...)` (sync/async/awaitable/timeout/ProviderError) while
  keeping the gevent timeout + consecutive-failure + auto-disable guard —
  queue behavior unchanged.
- Rerouted runtime capability invocation through the executor:
  - queue_service download chain (`invoke_provider` + `executor.run` for
    resolve_track_url)
  - app.py discography + enrich chains, navidrome scan trigger chain, auth
    guard authenticate
  - import_service discography + enrich chains
- Lifecycle hooks (on_load/on_enable/...) keep using call_safe (allowed by
  the brief's lifecycle exception). No `asyncio.run()` anywhere in providers.

## §4 — SDK boundary review

`fnack/plugin_api/models.py`, `context.py`, `events.py` are TRANSITIONAL
re-exports of internal `plugins.base`/`plugins.context`/`plugins.events`
classes. Documented as technical debt in AUTHORING.md + module docstrings:
they import cleanly and never pull app services/provider implementations, so
they don't block Phase 2; the contracts become standalone during extraction.
`capabilities.py`, `providers.py`, `errors.py`, `contracts.py` are real,
standalone public contracts today. No broad rewrite.

## §5 — Tests (Tests A–F)

- **A** (capability-specific priority): registry-level — one plugin at
  track.resolve 5 / track.metadata 30; ordering by per-cap priority, not
  plugin default.
- **B** (multiple capabilities = one plugin): exactly one registry handle +
  one loaded instance serving both caps.
- **C** (invalid declaration): media.scan registered, media.health +
  server.extension skipped; plugin still loaded + enabled.
- **D** (executor): sync, async, sync-def-returning-awaitable, timeout,
  ProviderError for missing method.
- **E** (official bundle): real bundled_plugins dir loads; download.track
  served by spotiflac < ytdlp; fingerprint/media/network/server.extension/
  notification all present (contract-validated).
- **F** (disabled plugin): ALL its capabilities disappear; re-enable brings
  them back.

## Files

- `plugins/models.py` — PluginCapabilityPriority table.
- `plugins/manager.py` — per-cap priority resolve/persist, contract
  validation, invoke_provider, capability_priorities in list_loaded.
- `plugins/api.py` — GET capabilities + POST capability priority endpoints.
- `fnack/plugin_api/capabilities.py` — ProviderHandle.priorities,
  providers_for(), priority_for(), deterministic ties.
- `fnack/plugin_api/contracts.py` — NEW single capability-contract mapping.
- `fnack/plugin_api/models.py` — SDK-boundary debt note.
- `app.py`, `services/queue_service.py`, `services/import_service.py` —
  provider invocations rerouted through the executor.
- `bundled_plugins/{deezer-batch,musicbrainz,itunes,navidrome}/plugin.json`
  — corrected capability declarations.
- `docs/plugins/AUTHORING.md` — capability-specific priority, validation,
  SDK boundary, new endpoints.
- `tests/architecture/*` — Tests A–F.
- `tests/run_smoke_test.py` — capability_priorities + new API assertions.
- fnack-plugins: 4 manifests corrected + repackaged (`45d613c`).

## Verification

- All 5 architecture tests PASSED (incl. new Test A/B/C/E/F + Test D case).
- `tests/run_smoke_test.py` PASSED (incl. per-cap priority API assertions).
- Live boot: registry correct, per-cap priorities reorder chains, plugin
  override preserved as default, zero-auth preserved, lidarr routes served.
- py_compile clean on all touched modules.

## Blocked by

(none)
