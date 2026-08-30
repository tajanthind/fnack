# Reconcile: metadata chain must be real priority iteration

wayfinder:grilling

## Question

Brief 3 §3 diagnosed the metadata chain in `app.py` `_sync_artist_discography_background`
and `import_service.py` as per-plugin-ID special-casing:

```python
for provider in _pm.get_metadata_providers():
    if provider.manifest.id != "fnack.deezer-batch":
        continue
    d = provider.get_artist_discography(str(deezer_artist_id))
    ...
```

Only `fnack.deezer-batch` was ever called — if that plugin is disabled, sync
silently fell through to the direct `deezer_service.get_artist_discography()`
call instead of the documented chain (Deezer 10 → MusicBrainz 20 → Spotify 30
→ iTunes 40, per `wayfinder/tickets/plugin-confirm-provider-chain.md`).

## Resolution

CONFIRMED and FIXED. Both call sites now iterate `get_metadata_providers()`
in priority order and the FIRST provider returning a usable discography wins;
the direct Deezer service call is the last resort (all providers
disabled/missing), preserving existing behavior. `fnack.itunes` now returns a
real album list from `get_itunes_artist_albums` (keyed by artist name — iTunes
has no stable public artist id in this flow), so the fallback genuinely works
when Deezer is disabled. MusicBrainz enrichment is routed through any provider
exposing `enrich` (capability check, not plugin-id check). The downloader loop
in `queue_service.py` was audited and is a REAL chain already (iterates
`get_downloaders()`, uses `can_handle`/`is_rate_limited`; `engine_gates` is
legacy `enable_spotiflac`/`enable_ytdlp` setting gating only, not chain
selection) — no change needed there. Verified: simulated chain with Deezer
absent → `fnack.itunes` serves the discography.

Reconciliation ticket for Brief 3. PR opened on
`plugin-architecture/reconcile-metadata-chain`.
