# Phase 1 (MASTER): Core SDK and Capability Registry

wayfinder:phase-1

## What this phase covers (01-PHASE-1-CORE-SDK-AND-CAPABILITY-REGISTRY.md)

Create the stable plugin boundary BEFORE moving provider implementations:

1. **Public SDK** — `fnack/plugin_api/` package (errors, capabilities,
   models, providers, context, events, version). Re-exports existing
   compatible models (TrackRef, DownloadResult, ...) — no duplicates.
2. **Capability constants + CapabilityRegistry** — provider-neutral contract
   IDs; registry separate from PluginManager (register/providers/has/
   unregister_plugin), priority-ordered (LOWEST number first, matching
   fnack's downloader/metadata chain semantics).
3. **Manifest capability declaration** — `capabilities` field, multiple per
   plugin allowed; when omitted, derived from `type`. Unknown IDs warn
   (forward-compatible), never fail.
4. **PluginManager public API** — `get_plugin`, `get_loaded`,
   `get_plugin_context`, `get_plugin_capabilities` (+
   `get_capability_providers` / `has_capability` / registry attr). Replaced
   ALL `_pm._plugins[...]` private access in app.py, plugins/api.py,
   queue_service.py.
5. **Async ProviderExecutor** — the ONE place that detects awaitables
   (`inspect.isawaitable`); sync + async methods, central `asyncio.run` in
   the executor only (never scattered through providers).
6. **Architecture tests** — 5 new files under `tests/architecture/`.

## Key decisions

- **Priorities stay core** (user instruction): `ProviderHandle.priority`,
  `providers()` sorted by (priority, plugin_id); priority_override flows
  through via `refresh_capability_registration` on the priority endpoint.
- **Registry reflects ENABLED providers only** (MASTER rule 2): registered
  on enable, unregistered on disable/unload. Disabled plugin -> capability
  disappears, no hidden fallback.
- **Capability derivation from `type`**: downloader -> download.track;
  metadata_provider -> artist.search/artist.discography/track.metadata/
  album.metadata; fingerprint -> fingerprint.identify; scan_trigger ->
  media.scan/media.connection_test; vpn -> network.route;
  server_extension -> server.extension; auth_provider -> auth.provider;
  event_hook -> notification.event. `track.resolve` is NOT implied by any
  type — plugins (fnack.spotify) declare it explicitly.
- **Official bundled manifests now declare `capabilities`** (data, not
  behavior) — 18 manifests updated, synced to fnack-plugins + repackaged
  (index.json carries capabilities).
- **Transitional imports frozen, not banned**: Phase 1 does not move
  providers (that's Phases 2-10). test_core_provider_independence.py
  enforces: SDK purity (hard), no private `_plugins` in core (hard), provider
  imports + provider-ID branches frozen to a documented TRANSITIONAL
  allowlist (each entry says which phase removes it). Adding a NEW
  hardwiring fails CI.
- **Interactive search split preserved**: /api/search-artist stays core
  calling the bundled Deezer provider (HARNESS §2 confirmed) — allowed entry
  in the transitional allowlist.
- **auth guard + server_extension blueprint loop** now read the capability
  registry (AUTH_PROVIDER / SERVER_EXTENSION) instead of iterating
  `_plugins` — verified live: zero-auth preserved (200 with no auth_provider
  enabled), lidarr routes registered via SERVER_EXTENSION, subsonic absent
  (disabled by default).

## Files

- `fnack/plugin_api/` — new public SDK package (8 modules).
- `plugins/base.py` — PluginManifest.capabilities field.
- `plugins/manager.py` — CapabilityRegistry instance, TYPE_CAPABILITIES
  derivation, lifecycle register/unregister, public API, executor.
- `plugins/api.py` — private `_plugins` -> public API; priority change
  refreshes registry ordering.
- `app.py` — auth guard + server_extension loop via capability registry.
- `services/queue_service.py` — `_pm._plugins[...]` -> `get_loaded`.
- `bundled_plugins/*/plugin.json` — `capabilities` declarations (18).
- `plugins/registry.py` + app.py marketplace seed — surface capabilities.
- `static/app.js` — capability badges on Installed + Marketplace.
- `docs/plugins/AUTHORING.md` — manifest `capabilities`, capability IDs,
  SDK section, executor/errors.
- `tests/architecture/` — 5 new architecture tests.
- `tests/run_smoke_test.py` — capabilities + public-API assertions.
- fnack-plugins: manifests synced, package_plugins.py carries
  capabilities, index repackaged (18 plugins).

## Verification

- `tests/run_smoke_test.py` PASSED.
- All 5 `tests/architecture/*` tests PASSED.
- Live boot: capability inventory correct (priority-ordered), zero-auth
  preserved, lidarr routes served via capability registry, subsonic
  capability absent while disabled.

## Blocked by

(none)
