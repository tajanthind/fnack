# Phase 4: Hardening, Tests, Isolation, and Final Deletion

<<<<<<< HEAD
wayfinder:phase-4
=======
- **Extraction 3 (MusicBrainz + iTunes) DONE — PR open**: 
  `services/musicbrainz_service.py` (251 lines) -> bundled_plugins/fnack.musicbrainz/musicbrainz.py
  (provider cache refactored to plugin-owned in-memory state — the legacy core
  DB cache model is gone; plugin imports no core models); `services/itunes_service.py`
  (311 lines) -> bundled_plugins/fnack.itunes/itunes.py; both plugins
  authoritative (import own modules, serve artist.search/discography/
  album.tracks + enrich). Sync/import enrichment routes through the plugin
  chain with NO hidden fallback to services.musicbrainz_service. itunes
  manifest declares album.tracks. independence allowlist shrank (app +
  import musicbrainz entries removed). new
  tests/architecture/test_musicbrainz_itunes_extraction.py. Smoke + 16 arch
  tests green. NOTE: branch based on origin/phase-4/extract-deezer (PR #28
  not yet merged); merge #28 then #29.
## Progress
>>>>>>> 18aaedc (refactor(plugin): extract MusicBrainz + iTunes into their providers (Phase 4, PR 3))

## What this covers (04-PHASE-4-HARDENING-TESTS-AND-DELETION.md)

The six remaining provider extractions (the official plugins are still
wrappers over legacy core services) + hardening/deletion + a final
documentation gate. Per the user directive, Phase 4 = **six extraction PRs +
one final documentation/architecture cleanup PR** (docs belong in each
extraction PR where natural).

## Extraction order (each = one branch+PR, never merged by me)

1. **fnack.spotify** — move `services/spotify_service.py` impl into
   `bundled_plugins/fnack.spotify/spotify.py`; plugin authoritative;
   delete `services/spotify_service.py`; remove legacy
   spotify_client_id/secret settings surface (app.py) — plugin owns them.
2. **fnack.deezer-batch** — move `services/deezer_service.py` impl into the
   plugin; delete the legacy service; migrate app.py get_artist_info
   (onboarding) + plugins/context.py facade + queue manual-path Deezer URL
   handling to the capability/metadata service.
3. **fnack.musicbrainz + fnack.itunes** — move `services/musicbrainz_service.py`
   + `services/itunes_service.py` impls into the plugins; delete both legacy
   services; migrate enrich_albums callers (app.py sync, import_service).
4. **fnack.acoustid** — move `services/acoustid_service.py` impl into the
   plugin; plugin exposes SDK `FingerprintProvider.identify(request) ->
   FingerprintEvidence`; delete legacy service; migrate app.py manual
   identify route + plugins/context.py facade to FingerprintService.
5. **fnack.navidrome** — move `services/navidrome_service.py` impl into the
   plugin; delete legacy service; migrate run_auto_split_repair
   (library task) + tag_normalization_service.py split-repair scan to the
   capability.
6. **Legacy settings deletion** — after each provider is proven migrated:
   remove legacy AppSetting reads/UI (spotiflac_quality/ytdlp_format/
   youtube_cookies_path/acoustid_api_key/navidrome_*/...); make plugin
   settings authoritative.
7. **Final documentation gate** — audit the whole repo for stale
   provider-service references; verify README/DEPLOY/plugin docs/capability
   docs/wayfinder/architecture docs describe the post-extraction plugin model
   (never "deprecated"); add a doc-reference grep as a regression test where
   practical.

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
