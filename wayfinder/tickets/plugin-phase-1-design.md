# Design: Phase 1 bundled plugin manifests + Settings → Plugins UI

wayfinder:research

## Resolution

RESOLVED (research) — findings written to `wayfinder/research/phase-1-bundled-plugins-design.md`. Full manifest inventory (spotiflac p10 downloader, ytdlp p50 + spotdl alias, deezer-batch/musicbrainz/spotify/itunes metadata providers with chain order + MusicBrainz pacing preserved, acoustid fingerprint, navidrome scan_trigger+settings_tab, vpn + http_proxy fold-in, 4 library_task scripts), auto-install flow (bundled_plugins_dir + idempotent InstalledPlugin rows, official/enabled/source_repo_id=None), Settings→Plugins new top-level page with grouped list + enable/disable + numeric priority_override column + health, write-lock discipline (health-log buffered, piggybacked flush), and 8 behavior-preservation callouts. Implementation starts after Phase 0 merges.

## Question

PHASE1_BUNDLED_PLUGINS_BRIEF §2/§3/§4 — design (no code changes yet) the exact
concrete artifacts Phase 1 will need, so implementation is mechanical once
Phase 0 merges:

1. **Bundled plugin inventory** — for each service in the table below,
   produce the concrete `plugin.json` manifest (id, name, type(s),
   permissions, settings_schema, ui.slots) and the thin plugin class that
   wraps the existing service function WITHOUT changing its behavior.
   Decision inputs (already user-confirmed):
   - `services/spotiflac_service.py` → downloader priority=10 (primary)
   - `services/ytdlp_service.py` → downloader priority=50 (fallback);
     spotdl is an ALIAS of this plugin (one fnack.ytdlp handles both entry
     points), not a separate row
   - `services/deezer_service.py` → metadata_provider priority=10 (batch
     path only; interactive /api/search-artist stays core)
   - `services/musicbrainz_service.py` → metadata_provider priority=20 with
     1 req/s pacing + Retry-After + negative cache preserved per-plugin
   - `services/spotify_service.py` → metadata_provider priority=30
   - `services/itunes_service.py` → metadata_provider priority=40
   - `services/acoustid_service.py` → fingerprint plugin, preserving the
     exact verify-when-unsure 0.8 gate + silent regional no-match + caution
     mark behavior (safety-relevant — do NOT simplify)
   - `services/navidrome_service.py` → scan_trigger + settings_tab
   - `services/vpn_service.py` → vpn plugin (+ `scripts/http_proxy.py` is
     the split-mode VPN CONNECT proxy — folds into this plugin)
   - `scripts/clean_navidrome_artists.py`, `normalize_album_tags.py`,
     `reverify_library.py`, `fix_navidrome_splits.py` → four library_task
     plugins; `scripts/run_maintenance.py` keeps working as a CLI that calls
     the same plugins
   - Core (NOT plugins): queue engine, models/auth/DB, plugin manager/bus/
     marketplace, import_service + metadata_service (orchestrators),
     verifier_service (safety-critical), interactive search route, caching
     layers. Lidarr/watcher: designed as library_source/event-driven types
     but NOT migrated in Phase 1 (deferred).
2. **Auto-install on fresh/existing DBs** (§3): startup `load_all()` flow that
   creates `InstalledPlugin` rows for every bundled plugin with
   trust_level=official, enabled=True, source_repo_id=None — WITHOUT requiring
   a marketplace visit. Where do bundled plugin folders physically live in the
   image (e.g. `/app/bundled_plugins/`) and how does the manager discover them
   alongside `/config/plugins/`?
3. **Settings → Plugins UI** (§4, user chose: new top-level tab/page):
   grouped-by-type list (Downloaders, Metadata Providers, Fingerprinting,
   Scan Triggers, Library Tasks, VPN, Event Hooks, Server Extensions, UI
   Extensions) with name/version/trust badge/enabled toggle/health; enable/
   disable wired to on_disable()/on_enable() + persisted InstalledPlugin
   flag; priority via NUMERIC input writing `priority_override` (new nullable
   column on InstalledPlugin or a PluginSetting key — decide which fits
   plugins/models.py better), ordered getters sort by override then manifest
   priority. Marketplace/Repositories tabs stubbed for Phase 3.
4. **Write-lock discipline** (from the scale-to-millions research): the UI and
   enable/disable endpoints must NOT add per-hook commits on the hot path.

Deliver: `wayfinder/research/phase-1-bundled-plugins-design.md` with the full
manifest inventory + plugin class skeletons + auto-install flow + UI
structure, plus a short "behavior-preservation callouts" list (anything the
migration touches that could change observable behavior, per PHASE1 §6.4).
