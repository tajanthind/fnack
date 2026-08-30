# Design: Phase 2 — cut the queue + metadata pipelines over to plugin chains

wayfinder:research

## Question

PLUGIN_ARCHITECTURE.md §10 Phase 2 + INTEGRATION.md §6: replace the hardcoded
downloader sequence in `queue_service._process_track_job` (currently
`resolve_spotify_url → download_track_spotiflac → _verify_or_rescue →
download_track_ytdlp → _verify_or_rescue`) with the plugin chain:

```python
track_ref = TrackRef(id=..., title=..., artist_name=..., album_name=...,
                     isrc=..., duration=..., spotify_url=..., deezer_id=...,
                     disc_number=..., track_number=...)
for downloader in plugin_manager.get_downloaders():   # priority-sorted
    if downloader.is_rate_limited() or not downloader.can_handle(track_ref):
        continue
    loaded = plugin_manager._plugins[downloader.manifest.id]
    result = plugin_manager.call_safe(loaded, "download", track_ref, dest_dir, options)
    if result and result.success:
        downloaded_file = result.file_path
        break
```

Design decisions to resolve (research — no code changes yet):

1. **Exact loop shape**: the INTEGRATION.md skeleton breaks on success but does
   NOT verify inside the loop. Today each downloader is verified right after
   its download (`_verify_or_rescue` per engine). Two options:
   (a) keep verify-per-downloader inside the loop (each plugin's result is
   verified before trying the next) — preserves today's behavior exactly,
   including which error message surfaces and the AcoustID rescue per engine;
   (b) verify once after a successful download. Recommend (a) — it's what the
   current code does and the brief demands behavior preservation.
2. **TrackRef construction**: which fields the queue has at that point
   (track/album/artist rows, isrc, spotify_url, duration) — map exactly.
3. **Options dict**: what each downloader plugin needs (spotiflac: quality +
   delay; ytdlp: format + audio_source + cookies; both: timeout). Settings
   come from the plugin's own settings store where moved in Phase 1, falling
   back to the legacy AppSetting rows for anything not yet migrated.
4. **Spotify URL resolution**: stays core (interactive-search decision);
   resolve_spotify_url runs before the chain exactly as today, feeding
   track_ref.spotify_url which `fnack.spotiflac.can_handle` gates on.
5. **Rate-limit semantics**: `fnack.spotiflac.is_rate_limited()` maps to the
   existing 429 circuit breaker — the chain's `is_rate_limited()` skip must
   produce the SAME "SpotiFLAC skipped (upstream rate limit circuit breaker)"
   failure_reason entry so the UI/history text is unchanged.
6. **Metadata enrichment chain**: `metadata_service`/sync currently call
   `deezer_service.get_artist_discography` + `musicbrainz_service.enrich_albums`
   + iTunes fallback. Cut over to `plugin_manager.get_metadata_providers()`
   (priority chain), with Deezer batch p10 authoritative, preserving the
   MusicBrainz pacing (already inside the service module). `enrich_albums`
   stays core glue that the chain's `musicbrainz` plugin exposes.
7. **Behavior-preservation callouts** (PHASE1 §6.4): identical failure_reasons
   strings, identical progress percentages (35/60), identical AcoustID rescue
   flow, identical socketio emissions. Any plugin disabled → chain skips it
   exactly as the old `enable_spotiflac`/`enable_ytdlp` settings toggles did.
8. **Fallback when ALL downloaders fail or are disabled**: current code has
   `failure_reasons.append("Both SpotiFLAC and yt-dlp engines are disabled in settings")`
   — replicate via a "no downloader could handle/run" message when the chain
   is empty or every plugin failed.

Deliver: `wayfinder/research/phase-2-pipeline-cutover.md` with the exact new
`_process_track_job` downloader section pseudocode, the metadata cutover
shape, the options/settings mapping table, and the behavior-preservation
checklist. No code changes.
