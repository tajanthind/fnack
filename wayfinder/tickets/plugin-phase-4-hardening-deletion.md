# Phase 4: Hardening, Tests, Isolation, and Final Deletion

wayfinder:phase-4

## What this covers (04-PHASE-4-HARDENING-TESTS-AND-DELETION.md)

The six provider extractions (official plugins were wrappers over legacy core
services) + hardening/deletion + a final documentation gate. Per the user
directive, Phase 4 = **six extraction PRs + one final documentation/
architecture cleanup PR** (docs belong in each extraction PR where natural).

## Extraction order (each = one branch+PR, never merged by me)

1. **fnack.spotify** — move `services/spotify_service.py` impl into the
   plugin; delete the legacy service; remove legacy spotify settings surface.
2. **fnack.deezer-batch** — move `services/deezer_service.py` impl into the
   plugin; delete the legacy service; add artist.info/album.search/
   track.search/album.tracks capabilities; migrate app/context/script callers.
3. **fnack.musicbrainz + fnack.itunes** — move both impls into the plugins;
   delete both legacy services; provider cache plugin-owned (no core models);
   no hidden enrichment fallback.
4. **fnack.acoustid** — move `services/acoustid_service.py` impl into the
   plugin; api_key plugin-owned; delete the legacy service; migrate app
   manual-identify + context facade.
5. **fnack.navidrome** — move `services/navidrome_service.py` impl into the
   plugin; config plugin-owned; delete the legacy service; migrate
   run_auto_split_repair + scripts.
6. **Legacy settings deletion** — after each provider is proven migrated:
   remove legacy AppSetting reads/UI; plugin settings authoritative.
7. **Final documentation gate** — audit the whole repo for stale
   provider-service references; verify README/DEPLOY/plugin docs/capability
   docs/wayfinder/architecture docs describe the post-extraction plugin model
   (never "deprecated"); add a doc-reference grep as a regression test;
   remove obsolete migration artifacts (e.g. the core MusicBrainzCache model).

## Documentation rule (user directive)

Documentation must describe the POST-EXTRACTION architecture, not merely say
an old service is "deprecated." E.g. after Spotify extraction:

- WRONG: "Spotify integration is implemented by services/spotify_service.py"
- RIGHT: "Spotify functionality is provided by the fnack.spotify plugin
  through the track.resolve capability."

Core documentation must make clear core does not contain provider
implementations. Each extraction PR updates, where applicable: README.md +
user-facing setup/usage docs, plugin docs/manifests, config/settings docs,
capability docs, installation/bundling docs, provider/plugin architecture
docs, migration notes, wayfinder phase/ticket docs, architecture/dev docs,
tests/architecture docs describing the old structure, comments/docstrings
that still describe a provider as a core service, and fnack-plugins docs for
the extracted plugin.

## Per-PR checklist (standing workflow)

one branch+PR per step; PR only (never merge/tag/bump version.py); smoke +
architecture tests green; live-boot verify; docs updated per the rule above;
fnack-plugins synced + repackaged; wayfinder + map updated.

## Progress

- **Extraction 1 (Spotify) DONE — PR #27**: `services/spotify_service.py`
  (573 lines) moved verbatim to `bundled_plugins/fnack.spotify/spotify.py`;
  plugin authoritative, serves track.resolve, owns client_id/secret settings
  (legacy AppSetting surface removed from app.py); docs updated to
  post-extraction architecture; test_plugin_boundary probe now imports the
  plugin module; new tests/architecture/test_spotify_extraction.py.
  fnack-plugins synced (883b981). Smoke + 14 arch tests green.
- **Extraction 2 (Deezer) DONE — PR #28**: `services/deezer_service.py`
  (455 lines) moved verbatim to `bundled_plugins/fnack.deezer-batch/deezer.py`;
  plugin authoritative, serves artist.search/artist.discography/artist.info/
  track.metadata/album.metadata/album.search/track.search/album.tracks (new
  SDK capabilities ARTIST_INFO/ALBUM_SEARCH/TRACK_SEARCH/ALBUM_TRACKS);
  MetadataService gained get_artist_info/search_album/search_track/
  get_album_tracks; app.py api_add_artist routes via MetadataService (fixes a
  latent NameError from Phase 3); plugins/context.py facade +
  scripts/reverify_library.py migrated; itunes fallback inside deezer.py lazy
  + guarded. new tests/architecture/test_deezer_extraction.py. Smoke + 15
  arch tests green.
- **Extraction 3 (MusicBrainz + iTunes) DONE — PR #29**:
  `services/musicbrainz_service.py` (251 lines) -> bundled_plugins/fnack.musicbrainz/musicbrainz.py
  (provider cache refactored to plugin-owned in-memory state — plugin imports
  no core models); `services/itunes_service.py` (311 lines) ->
  bundled_plugins/fnack.itunes/itunes.py; both plugins authoritative; sync/
  import enrichment routes through the plugin chain with NO hidden fallback;
  itunes manifest declares album.tracks; new
  tests/architecture/test_musicbrainz_itunes_extraction.py. Smoke + 16 arch
  tests green.
- **Extraction 4 (AcoustID) DONE — PR #30**: `services/acoustid_service.py`
  (261 lines) -> bundled_plugins/fnack.acoustid/acoustid.py; api_key
  refactored to PLUGIN-OWNED (injected via set_api_key(), no core models/DB);
  plugin authoritative, serves fingerprint.identify, exposes
  identify_candidates/verify_download/last_lookup_flags; app.py manual-
  identify route resolves the plugin via the fingerprint.identify capability;
  plugins/context.py verify_download_acoustid resolves through the plugin;
  fnack.ytdlp fallback no longer references acoustid_service. new
  tests/architecture/test_acoustid_extraction.py. Smoke + 16 arch tests green.
- **Extraction 5 (Navidrome) DONE — PR #31**: `services/navidrome_service.py`
  (280 lines) -> bundled_plugins/fnack.navidrome/navidrome.py (config
  refactored to PLUGIN-OWNED injection — trigger_navidrome_scan(config) /
  run_auto_split_repair(config); no core AppSetting reads); plugin
  authoritative, serves media.scan/health/connection_test, exposes
  run_split_repair; app.py fix-splits route + run_maintenance resolve through
  the plugin; fix_navidrome_splits imports the plugin module; tag_normalization
  triggers scan via the plugin. new tests/architecture/test_navidrome_extraction.py.
  Smoke + 17 arch tests green.
- **Extraction 6 (Documentation gate) DONE — PR open**: the obsolete core
  `MusicBrainzCache` DB model removed (provider cache is plugin-owned);
  README Architecture section restored to post-extraction form (core
  provider-free, providers = plugins + capabilities); independence-test
  transitional allowlist now EMPTY (no provider-service imports remain in
  core); wayfinder map + ticket mark Phase 4 complete; new
  tests/architecture/test_documentation_gate.py asserts current-state docs
  never name deleted services, core imports none, README lists all provider
  plugins, obsolete model gone, wayfinder marks completion. Smoke + 19 arch
  tests green. (Delivery note: PR #29 merged into its base branch
  `phase-4/extract-deezer`, so main only got the merge commit — the
  MB+iTunes extraction files are delivered to main by `fix/mb-itunes-to-main`
  (PR #33). Merge #33 before the doc-gate PR #32.)

- **Documentation pass (README) DONE** — user review: the doc gate proved
  the README wasn't *architecturally wrong* but it wasn't *good* docs. The
  README was rewritten to lead with the architecture (core → application
  service → capability → provider registry → plugin, with the defining
  rules: multi-capability plugins, multi-provider capabilities, per-
  capability priority, disable-removes-capabilities, zero-providers valid,
  official bundled, community replacement, provider-neutral verification).
  Configuration split into Core vs Plugin tables (provider settings no
  longer presented as core); the Subsonic (server.extension) vs Navidrome
  (media.scan) distinction is explicit; stale implementation language
  ("cached Deezer lookups", "Subsonic API Integration") removed. The doc
  gate test gained structural assertions for these classes of issues.
- **Final cleanup (essential-plugin packaging + user-focused docs) DONE —
  PR open** (branch `fix/final-docs-and-essential-plugin-packaging`, one PR
  per the user directive): README rewritten USER-focused and
  architecture-light (no architecture section, no official-plugin inventory,
  no per-plugin config enumeration; quick start + config + plugins +
  guides); the long YouTube-cookies and VPN guides moved to
  `docs/guides/youtube-cookies.md` + `docs/guides/vpn.md`; deep architecture
  moved to the new `docs/architecture.md` (flow, rules, capability list,
  official-plugin snapshot, essential-vs-optional packaging policy);
  `plugins/essential.py` `ESSENTIAL_PLUGINS` is the SINGLE source of truth
  for what the Docker image bakes — fnack.spotiflac + fnack.ytdlp +
  fnack.spotify + fnack.deezer-batch (the first-run download/sync
  workflow: artist.search/discography, track.resolve, download.track with
  primary + fallback); the Dockerfile prunes `bundled_plugins/` to that set
  via `scripts/select_essential_plugins.py` so the image auto-installs only
  essential plugins, while every other official plugin stays installable
  from the Marketplace and core has no dependency on any optional plugin;
  arch tests cleaned of transitional language/allowlists (boundary test now
  pins the ONLY remaining services.* helpers — verifier_service +
  vpn_service — and fails fast on stale entries like the deleted
  acoustid_service; fingerprint_service comments no longer promise a pending
  AcoustID extraction); new `tests/architecture/test_essential_plugins.py`
  pins the packaging policy (vendored essential dirs, optional-not-essential,
  Dockerfile prunes, selection script functional check, first-run coverage);
  fnack-plugins gains `tests/test_manifest_index_parity.py` (deterministic
  manifest ↔ index parity, plain python) and its README gains the missing
  fnack.lidarr row + essential/optional note. Smoke + 20 arch tests green.

## Final state (Phase 4 complete)

- All six legacy provider services deleted: spotify, deezer, musicbrainz,
  itunes, acoustid, navidrome.
- Official plugins are AUTHORITATIVE implementations (impl + settings + state
  + cache in the plugin); core has zero provider imports and zero
  provider-ID branches (verified by architecture tests).
- No hidden provider fallbacks; zero providers for a capability is a valid
  state (CapabilityUnavailable).
- Documentation (README, DEPLOY, AUTHORING, wayfinder) describes the
  post-extraction plugin model.
