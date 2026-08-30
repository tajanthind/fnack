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

## Roadmap (execution carried into the map)

1. **Phase 0** (`plugin-architecture/phase-0-scaffold`): drop `plugins/` + `examples/`
   into the repo, `packaging>=23.0` in requirements, register plugin tables +
   manager + blueprint + `plugin_slot()` helper in `app.py`, emit the additive
   events in `queue_service.py`, adapt `tests/run_smoke_test.py` to the real
   models, write `docs/plugins/AUTHORING.md`. Zero behavior change.
   **DONE** — PR #1 open; 11 commits; smoke test + image build green in CI
   (PR checks CLEAN); live-container E2E verified (discovery/import/listing,
   full REST lifecycle enable/settings/disable, plugins dir creation, config
   restored pristine); full validation run on the real library this session
   (6 artists intact, stats identical, zero errors). Live finding recorded:
   manual-install enable needs InstalledPlugin row creation (Phase 1 fix,
   research §6). Awaiting user merge before Phase 1 (HARNESS §0).
2. **Phase 1** (`plugin-architecture/phase-1-bundled-plugins`): bundle the
   downloaders/metadata-providers/fingerprint/scan_trigger/vpn/library_task
   plugins, auto-install on startup with `trust_level=official`, Settings →
   Plugins UI (grouped list, enable/disable, priority override), scale-to-millions
   (denormalized artist counters, paginated `/api/artists`, FTS5 if LIKE scans
   found). Behavior-preserving.
3. **Phase 2** (`plugin-architecture/phase-2-pipeline-cutover`): replace
   hardcoded spotiflac→ytdlp and metadata call sequences with plugin chains.
4. **Phase 3** (`plugin-architecture/phase-3-marketplace`): repositories,
   marketplace install/update/uninstall, per-plugin settings, health dashboard.
5. **Phase 4** (`plugin-architecture/phase-4-stretch`): auth_provider (SSO),
   Discord + ntfy event_hook pack, Subsonic server_extension, per-plugin Python
   dep isolation, signed manifests.

## Not yet specified

- Phase 3/4 details beyond the spec (§5, §11) — blocked on Phases 0–1 landing.
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
