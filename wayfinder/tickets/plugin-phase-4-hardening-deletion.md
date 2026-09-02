
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

- **Extraction 1 (Spotify) DONE — PR #27 open**: `services/spotify_service.py`
  (573 lines) moved verbatim to `bundled_plugins/fnack.spotify/spotify.py`;
  plugin authoritative, serves track.resolve, owns client_id/secret settings
  (legacy AppSetting surface removed from app.py); docs updated to
  post-extraction architecture (README architecture section, DEPLOY pipeline,
  AUTHORING); test_plugin_boundary probe now imports the plugin module; new
  tests/architecture/test_spotify_extraction.py. fnack-plugins synced
  (883b981). Smoke + 14 arch tests green.
- **Extraction 2 (Deezer) DONE — PR open**: `services/deezer_service.py`
  (455 lines) moved verbatim to `bundled_plugins/fnack.deezer-batch/deezer.py`;
  plugin authoritative, serves artist.search/artist.discography/artist.info/
  track.metadata/album.metadata/album.search/track.search/album.tracks (new
  SDK capabilities ARTIST_INFO/ALBUM_SEARCH/TRACK_SEARCH/ALBUM_TRACKS added
  to capabilities.py + contracts.py; MetadataService gained get_artist_info/
  search_album/search_track/get_album_tracks). app.py api_add_artist routes
  via MetadataService (fixes latent NameError from Phase 3); plugins/context.py
  facade + scripts/reverify_library.py migrated; queue/import comments
  updated; itunes fallback inside deezer.py made lazy + guarded (sibling
  module import). Legacy deezer settings surface not present (none existed
  beyond the app.py read, removed in Phase 3). docs updated (DEPLOY pipeline,
  README metadata sources). independence allowlist shrank (app deezer +
  deezer_service->itunes entries removed). new
  tests/architecture/test_deezer_extraction.py. Smoke + 15 arch tests green.
