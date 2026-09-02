# fnack plugin architecture: harness + bundled first-party plugins

**Tracker**: local-markdown. Map = this file. Tickets = `tickets/plugin-*.md`, one per
decision, each carrying a `wayfinder:<type>` label line. Research findings =
`research/<ticket-name>.md`. Blocking is expressed in the ticket body (`Blocked by:` line).

## Destination

The plugin system fully specified in `/home/tajanthind/overhaul-brief/`
(`HARNESS_BRIEF.md` + `PHASE1_BUNDLED_PLUGINS_BRIEF.md` +
`PLUGIN_ARCHITECTURE.md` + `INTEGRATION.md` + scaffold zip) lands in the real
repo, phase by phase: Phase 0 (framework scaffold wired per `INTEGRATION.md`,
zero behavior change, `docs/plugins/AUTHORING.md` shipped with it), Phase 1
(today's services become bundled/official/enabled-by-default plugins with a
Settings → Plugins management UI + scale-to-millions work), Phase 2 (queue +
metadata pipelines cut over to plugin chains), Phase 3 (repository marketplace),
Phase 4 (stretch: auth_provider, webhook pack, server_extension Subsonic, dep
isolation). Each phase is one branch + PR off the current tip, merged only on
user approval, behavior-preserving, `tests/run_smoke_test.py` green after every
phase.

## Notes

- Domain: self-hosted music downloader (Flask + gevent + SQLite WAL, single
  container, zero required auth). Standing preferences: no second container;
  everything in this repo (`/home/tajanthind/fnack 2`); tagged releases after
  approval.
- **Execution mode**: the briefs override wayfinder's plan-don't-do default —
  this map carries implementation into the phases below. Spec is
  `PLUGIN_ARCHITECTURE.md` + `INTEGRATION.md` + the scaffold; treat them as the
  spec, not a sketch.
- Branching rule (HARNESS §0): never commit to `main`; one branch per phase
  (`plugin-architecture/phase-0-scaffold`, `plugin-architecture/phase-1-bundled-plugins`,
  …) off the current tip; push + PR; **do not merge or tag or touch `version.py`**
  without user approval.
- Core/plugin decision rule (HARNESS §1): stays core if it is (1) on a
  latency-sensitive synchronous user-facing path where plugin indirection would
  regress UX, (2) required for minimal usability/safety with zero plugins
  installed, or (3) infrastructure the plugin system itself depends on.
  Everything else becomes a plugin, even bundled-by-default.
- AcoustID/MusicBrainz tuned behavior must survive migration unchanged
  (`wayfinder/tickets/acoustid-fingerprinting.md`, `regional-artist-fallback.md`,
  `musicbrainz-integration.md`): 0.8 verify gate, silent regional no-match,
  caution flag, Deezer authoritative, 1 req/s MusicBrainz throttle.
- Plugins never import `models.py`/`app.py`/`services/*` — only `PluginContext`.
  Extend `context.py`, don't reach around it.
- Audit finding (this session): `scripts/http_proxy.py` IS the split-mode VPN
  CONNECT proxy (spawned by the vpn machinery) — folds into the vpn plugin, not
  a `library_task`. `scripts/make_release.sh` + `scripts/sysctl-shim` are build
  infra, not plugins.

## Decisions so far

<!-- one line per resolved ticket; zoom the ticket for the detail -->

- [Confirm: Deezer interactive search stays core; batch enrichment becomes the plugin](tickets/plugin-confirm-deezer-search-split.md):
  `/api/search-artist` stays core calling the bundled Deezer provider directly;
  `metadata_provider` plugin wraps only the batch/import path, sharing the same
  underlying function.
- [Confirm: metadata_provider chain order + MusicBrainz throttle preservation](tickets/plugin-confirm-provider-chain.md):
  Deezer (10) → MusicBrainz (20, enrichment-only, 1 req/s + Retry-After + negative
  cache preserved per-plugin) → Spotify (30) → iTunes (40); `enrich_albums` stays
  core glue.
- [Decide: spotdl plugin form + priority](tickets/plugin-spotdl-form.md):
  spotdl is an alias of the yt-dlp downloader plugin (one `fnack.ytdlp` handles
  both entry points); no separate spotdl row; flag any dead-code deletion.
- [Decide: orchestrators (import/metadata), verifier, lidarr, watcher](tickets/plugin-orchestrators-verifier-lidarr-watcher.md):
  import + metadata are CORE orchestrators; verifier is CORE safety-critical;
  Lidarr → new `library_source` type; watcher → own event-driven type or core.
- [Decide: extra plugin types to fold into the manifest schema now](tickets/plugin-extra-types.md):
  fold lyrics_provider, storage_backend, auth_provider, conflict_resolver,
  library_source into the `type` enum now (no Phase-1 impls); `context.fs` stays
  concrete in Phase 1; Discord + ntfy as separate `event_hook` plugins.
- [Decide: priority-override UI](tickets/plugin-priority-ui.md):
  numeric priority input per plugin row (writes `priority_override`); no
  drag-reorder in Phase 1.
- [Design: Phase 1 bundled plugin manifests + Settings → Plugins UI](tickets/plugin-phase-1-design.md):
  research — full manifest inventory, plugin class skeletons, auto-install
  flow, UI structure, behavior-preservation callouts (findings in
  `research/phase-1-bundled-plugins-design.md`).
- [Reconciliation: metadata chain must be real priority iteration (Brief 3)](tickets/plugin-metadata-chain-reconciliation.md):
  the sync/import discography fetch was per-plugin-ID special-casing (only
  `fnack.deezer-batch` ever served). Fixed to iterate
  `get_metadata_providers()` in priority order (Deezer p10 → MusicBrainz p20
  → Spotify p30 → iTunes p40) with the FIRST provider returning a usable
  discography winning, and the direct Deezer service call only as a last
  resort. `fnack.itunes.get_artist_discography` now returns a real album
  list (keyed by artist name) so the fallback is genuine when Deezer is
  disabled. The downloader loop was audited: it IS a real chain (no
  hardcoded-ID skip; per-provider gates removed in Phase 2 PRs 3+4 — the
  capability registry is the only enable/disable mechanism).
- [Decide: bundled-plugin single source of truth](tickets/plugin-bundled-sync-of-truth.md):
  `tajanthind/fnack-plugins/plugins/` is the SOURCE of truth for first-party
  plugin code; fnack's `bundled_plugins/` is the vendored copy baked into the
  image. Release process: edit + package in `fnack-plugins` (run
  `package_plugins.py` to regenerate `index.json` + `dist/`), then copy the
  same plugin sources into fnack's `bundled_plugins/` before tagging a core
  release. Verified identical today (all 17 plugins byte-for-byte). The
  auto-seeded official repo URL points at the fnack-plugins `index.json`.

- [Phase 5: Settings-surface gaps (VPN/yt-dlp), plugin descriptions, reconciliation verification](tickets/plugin-phase-5-settings-and-descriptions.md):
  §1 metadata-chain fix confirmed live on main (stale-blob audit artifact);
  VPN gets a custom settings_tab (upload/start/stop/status via core /api/vpn
  routes, Option B — schema-only can't express file+actions+status); yt-dlp
  cookies_file via the reusable per-plugin file upload; plugin descriptions
  surfaced on /plugins (API + Installed-row render; Marketplace already had
  it). Ported the prior-session VPN/file-upload work that had missed the
  PR #6 merge. Live-verified end to end.
- [Verify: metadata-chain fix from PR #6 is live on main](tickets/plugin-metadata-chain-live-verification.md):
  confirmed intact (per-provider keying + served_by + try/except in app.py
  and import_service.py at `6f4d043`); audit's old-version reading was a
  stale GitHub blob.

- [Phase 6: unified plugin-settings modal, in-place updates, version-mismatch state, descriptions](tickets/plugin-phase-6-unified-settings-modal.md):
  §1 all three (VPN/Navidrome/Subsonic) confirmed on ONE render path
  (settings_tab slot); §2 unified modal — file schema type, manifest
  `actions` array + /action/<id> route, /status endpoint, VPN/Navidrome/
  Subsonic migrated, zero inline cards; §3 in-place update (bundled refuse —
  they update with the image; marketplace plugins update independently,
  settings survive); §4 visible 'Unsupported — requires core ≥ X, you're on
  Y' for version-incompatible plugins (load_error in list + greyed
  Marketplace); §5 descriptions confirmed on Installed + Marketplace.
  Live-verified end to end.
- [Phase 7: core-hardcoding audit, Lidarr extraction, 0.2.x-parity trim, scheduled failed retry](tickets/plugin-phase-7-core-trim-and-hardcoding.md):
  §1a metadata chain = false alarm (stale blob again — fix live on main);
  §1b Lidarr emulation extracted to bundled `fnack.lidarr` plugin
  (`library_source` + `server_extension`, routes behind the plugin,
  `services/lidarr_service.py` deleted, context methods added); §1c
  navidrome test/scan now go through `get_scan_triggers()` (fallback direct),
  yt-dlp cookies classified core (download-pipeline config surface);
  §2 was stale (Phase 6 shipped it); §3 `default_disabled` set (subsonic,
  discord/ntfy webhooks, reverse-proxy-auth — 0.2.x non-parity plugins now
  opt-in); §5 `_retry_all_failed()` + `_periodic_failed_retry_loop` +
  `retry_interval` setting (default daily) + settings UI. Smoke test green
  (multi-type fixture covers the new plugin shape).
- [MASTER Phase 1: Core SDK + Capability Registry](tickets/plugin-phase-1-master-sdk-capability-registry.md):
  new public `fnack/plugin_api/` SDK (errors/capabilities/models/providers/
  context/events/version); CapabilityRegistry separate from PluginManager
  (priority-ordered, priorities stay core); manifest `capabilities` field
  (multiple per plugin, derived from `type` when omitted) on all 18 bundled
  plugins + fnack-plugins; PluginManager public API replacing every
  `_pm._plugins[...]` private access (app.py auth guard + server_extension
  loop, api.py, queue_service.py); ProviderExecutor (sync + async via
  `inspect.isawaitable`, central asyncio.run); 5 architecture tests;
  transitional provider imports/ID-branches frozen to a documented allowlist
  (each entry names the removal phase). Live-verified: capability inventory
  correct, zero-auth preserved, disabled plugin capability disappears.
- [MASTER Phase 1.1: capability-specific priority + provider contract validation](tickets/plugin-phase-1-1-remaining-architecture-fixes.md):
  §1 priority per (plugin_id, capability_id) — PluginCapabilityPriority
  table, resolution chain (cap-specific > plugin-level override > manifest;
  LOWEST = tried first), plugin-level overrides preserved as defaults (no
  migration rewrite); registry providers_for()/priority_for() with
  deterministic (priority, plugin_id) ties; API GET /capabilities + POST
  /capabilities/<cap>/priority; UI follow-up documented; §2 capability
  contracts validated at load (fnack.plugin_api.contracts = single mapping;
  invalid caps skipped with clear warning, valid caps still load) — official
  manifests corrected (deezer-batch/musicbrainz/itunes dropped
  album.metadata, navidrome dropped media.health), synced to fnack-plugins;
  §3 ProviderExecutor = runtime invocation boundary (invoke_provider on the
  manager; queue/app/import chains rerouted; lifecycle stays under
  call_safe); §4 SDK re-export debt documented; §5 Tests A–F added. All
  tests green, smoke green.


- [MASTER Phase 2 (PR 3): DownloadService + SpotiFLAC extraction](tickets/plugin-phase-2-spotiflac.md):
  fnack.spotiflac becomes the authoritative implementation — provider code
  moved into the plugin (spotiflac.py), plugin implements the FINAL SDK
  TrackDownloader contract (request-object based, async); queue DownloadService
  gains a migration adapter (SDK + legacy providers coexist until PR 4);
  manual-download path routes through the provider boundary; vpn_service
  emits network.route_changed instead of importing the provider;
  services/spotiflac_service.py deleted; legacy settings migrate into the
  plugin store; multi-file plugin support in the manager; parity test added;
  independence-test allowlist shrank. Smoke + 6 architecture tests green.


- [Phase 2 (PR 4): yt-dlp provider extraction](tickets/plugin-phase-2-ytdlp.md):
  fnack.ytdlp becomes the authoritative implementation — engine moved
  verbatim into the plugin (ytdlp.py), plugin implements the FINAL SDK
  TrackDownloader contract; DownloadRequest gains provider-neutral hints
  (query/cookies_path/audio_source/check_duration); queue adapter carries
  the hints and normalizes the SDK result; the 5 manual-path
  download_track_ytdlp calls route through the provider boundary; cookies
  settings UI routes through the provider (no services import in app.py);
  services/ytdlp_service.py + spotdl_service.py deleted; legacy settings
  migrate into the plugin store; verifier/acoustid helpers exposed through
  the PluginContext facade; parity test added; independence/boundary-test
  allowlists updated. Smoke + 8 architecture tests green.


- [Phase 3: Application Services, Verification, and Queue decoupling](tickets/plugin-phase-3-application-services.md):
  the new 03 brief renumbers Phase 3 as the application-services layer
  (the marketplace Phase 3 below is separate/landed). `queue_service.py`
  becomes an orchestrator: DownloadService / MetadataService /
  FingerprintService / VerificationService / MediaServerService each resolve
  capabilities and own provider policy; queue + API routes call services,
  never provider services; verification is provider-neutral (normalized
  evidence, no acoustid_match rules in core); zero providers → structured
  CapabilityUnavailable; candidate configs supported by
  test_connection(candidate_config). One branch+PR per service.
  **Step 1 (DownloadService) + Step 2 (MetadataService) done** — PRs #21/#22
  (queue + app/import metadata callers migrated; tag-normalization service
  renamed to tag_normalization_service.py; fnack.spotify + fnack.deezer-batch
  synced). Smoke + 10 architecture tests green.
  **Step 3 (FingerprintService + VerificationService) done** — PR #23
  (provider-neutral verification: normalized FingerprintEvidence via
  fingerprint.identify; VerificationResult with metadata+fingerprint
  evidence; queue _verify_or_rescue routes through the service, no
  acoustid_service import in core; SDK MetadataEvidence/TrackMatch/
  VerificationResult). Smoke + 11 architecture tests green.
  **Step 4 (MediaServerService) done** — PR #24 (media.scan/health/
  connection_test via the registry; candidate-config test_connection
  forwards unsaved settings; app/queue navidrome scan+test callers migrated;
  navidrome plugin declares media.health; no navidrome import in queue).
  Smoke + 12 architecture tests green.
  **Step 5 (Queue/API cleanup) done** — PR #25: queue is a pure orchestrator
  (generic core + application services only; zero provider imports/IDs);
  app routes use application services (remaining provider imports documented
  transitional); test_phase3_completion.py asserts all brief completion
  criteria. **PHASE 3 COMPLETE** — Smoke + 13 architecture tests green.


- [Phase 4: Hardening, Tests, Isolation, and Final Deletion](tickets/plugin-phase-4-hardening-deletion.md):
  the official plugins are now AUTHORITATIVE implementations — each of the
  six legacy provider services (spotify, deezer, musicbrainz, itunes,
  acoustid, navidrome) moved into its plugin (impl + settings + state +
  cache), and `services/*_service.py` is deleted. No hidden provider
  fallbacks; zero providers for a capability is a valid state. Each
  extraction PR updated its docs to the post-extraction plugin model
  (README/DEPLOY/AUTHORING/wayfinder — never "deprecated"). Final
  documentation gate: obsolete core `MusicBrainzCache` model removed,
  independence-test transitional allowlist now EMPTY, and
  `tests/architecture/test_documentation_gate.py` greps current-state docs
  for stale provider-service references as a regression test.
  **PHASE 4 COMPLETE** — Smoke + 18 architecture tests green.

## Roadmap (execution carried into the map)

1. **Phase 0** (`plugin-architecture/phase-0-scaffold`): drop `plugins/` + `examples/`
   into the repo, `packaging>=23.0` in requirements, register plugin tables +
   manager + blueprint + `plugin_slot()` helper in `app.py`, emit the additive
   events in `queue_service.py`, adapt `tests/run_smoke_test.py` to the real
   models, write `docs/plugins/AUTHORING.md`. Zero behavior change.
   **DONE** — merged 2026-08-30 as PR #1 (commit `454696c`); smoke test + image build green in CI (PR checks CLEAN); live-container E2E verified (discovery/import/listing, full REST lifecycle enable/settings/disable, plugins dir creation, config restored pristine); validation run on the real library (6 artists intact, stats identical, zero errors). Live finding recorded: manual-install enable needs InstalledPlugin row creation (fixed in Phase 1, research §6).
2. **Phase 1** (`plugin-architecture/phase-1-bundled-plugins`): bundle the
   downloaders/metadata-providers/fingerprint/scan_trigger/vpn/library_task
   plugins, auto-install on startup with `trust_level=official`, Settings →
   Plugins UI (grouped list, enable/disable, priority override), scale-to-millions
   (denormalized artist counters, paginated `/api/artists`, FTS5 if LIKE scans
   found). Behavior-preserving.
   **DONE (in PR)** — 13 bundled plugins auto-install (official/enabled,
   correct chain order: spotiflac p10, ytdlp p50, deezer p10 → musicbrainz p20
   → spotify p30 → itunes p40); /plugins top-level page (grouped list, toggle,
   numeric priority → priority_override persisted across restart); Artist
   counters denormalized + backfilled (match GROUP BY ground truth), /api/artists
   paginated, /api/queue capped; health-log buffered flush; priority-override
   row-creation fix verified live. FTS5 deferred (no user-facing LIKE scans).
3. **Phase 2** (`plugin-architecture/phase-2-pipeline-cutover`): replace
   hardcoded spotiflac→ytdlp and metadata call sequences with plugin chains.
   **DONE (in PR)** — downloader chain loop (spotiflac p10 → ytdlp p50,
   per-engine verify, dedup-copy skip guard), metadata chain (deezer-batch p10
   → musicbrainz enrich) in sync/import, per-plugin settings UI (settings_schema
   modal per plugin, namespaced PluginSetting rows), call_safe BaseException
   + 600s download timeout fix (stuck-job bug). Live-verified: FLAC preserved
   via dedup skip, clean failures with preserved strings, no stuck jobs.
4. **Phase 3** (`plugin-architecture/phase-3-marketplace`): repositories,
   marketplace install/update/uninstall, per-plugin settings, health dashboard.
   **DONE (in PR)** — tabbed /plugins page (Installed / Marketplace /
   Repositories); marketplace grid with trust/compat/bundled markers;
   add/refresh/remove repos; install/update/uninstall with Community
   trust modal; bundled install/uninstall refused server-side; E2E
   live-verified (add repo → sha256 install → settings → restart persist →
   uninstall → cleanup).
5. **Phase 4** (`plugin-architecture/phase-4-stretch`): auth_provider (SSO),
   Discord + ntfy event_hook pack, Subsonic server_extension, per-plugin Python
   dep isolation, signed manifests.
   **DONE (in PR)** — queue events job_completed/job_failed/caution_flagged;
   Discord + ntfy webhook plugins (live-verified: Discord embed captured on
   real download); config-as-code export/import (secrets redacted);
   reverse-proxy-auth opt-in (zero-auth preserved by default, verified
   401-without/200-with header); Subsonic API (ping/getArtists/getAlbumList2/
   stream — verified FLAC streaming + token auth); per-plugin dep isolation
   (pip --target + sys.path). Signed manifests + update channels deferred
   (post-Phase-4 optional).
6. **Phase 7** (`plugin-architecture/phase-7-core-trim-and-hardcoding`):
   core-hardcoding audit (HARNESS BRIEF 7). §1a metadata chain re-verified
   (false alarm — fix live on main); §1b Lidarr emulation extracted to
   bundled `fnack.lidarr` plugin (`library_source` + `server_extension`,
   routes behind the plugin, `services/lidarr_service.py` deleted, context
   methods added); §1c navidrome test/scan routed through
   `get_scan_triggers()` (direct fallback kept), yt-dlp cookies classified
   core; §2 already shipped (Phase 6); §3 `default_disabled` set —
   plugins that didn't exist in 0.2.x (subsonic, discord/ntfy webhooks,
   reverse-proxy-auth) now ship disabled-by-default, still listed opt-in;
   §5 scheduled failed-song retry (`_retry_all_failed()` shared with the
   manual button, `_periodic_failed_retry_loop`, `retry_interval` default
   daily, settings UI). Smoke test green (multi-type fixture); PR #10.
   **DONE (in PR)** — merged by user, container redeployed for live E2E.

## Not yet specified

- Phase 2 pipeline-cutover mechanics (INTEGRATION.md §6) — now ticketed:
  [Design: Phase 2 — cut the queue + metadata pipelines over to plugin chains](tickets/plugin-phase-2-design.md)
  (research in progress; implementation after PR #2 merges).
- Phase 3/4 details beyond the spec (§5, §11) — blocked on Phases 0–2 landing.
- Exact `context.*` additions needed by specific service migrations (raised per
  service during Phase 1, per HARNESS §5.5).
- Whether `scan_trigger`/`fingerprint` need user-facing priority ordering
  (only if >1 active implementation can exist).

## Out of scope

- Switching off SQLite / moving to Postgres (PHASE1 §5.6 — documented non-goal).
- Rewriting downloader/verifier/AcoustID behavior during migration (must stay
  behavior-preserving).
- The completed reliability/catalogue effort (see `map.md` — its tickets stay
  closed; referenced as background for AcoustID/MusicBrainz behavior).
