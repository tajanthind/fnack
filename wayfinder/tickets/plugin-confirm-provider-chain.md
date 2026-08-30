# Confirm: metadata_provider chain order + MusicBrainz throttle preservation

wayfinder:grilling

## Question

PHASE1_BUNDLED_PLUGINS_BRIEF §2 row for `itunes_service.py`,
`musicbrainz_service.py`, `spotify_service.py` (and `deezer_service.py` as
batch provider): "Chain order: confirm with me — wayfinder/tickets/
musicbrainz-integration.md already establishes MusicBrainz as enrichment-only
with Deezer authoritative and a 1 req/s throttle; preserve that ordering and
the throttle behavior exactly."

Proposed chain (ascending priority = tried first):
- Deezer (batch enrichment) — priority 10, default top (authoritative)
- MusicBrainz — priority 20, enrichment-only, 1 req/s pacing + Retry-After
  backoff preserved verbatim for that plugin
- Spotify — priority 30
- iTunes — priority 40 (fallback album-track source)

Decision to confirm:
1. The order above (Deezer → MusicBrainz → Spotify → iTunes).
2. MusicBrainz's rate limiting is enforced per-plugin (its own `context.http`
   usage carries the 1 req/s pacing + negative-cache behavior), not just "one
   more provider in the list".
3. `enrich_albums` stays called only from sync/import orchestration (core
   glue), with the plugin providing the underlying lookup.

## Resolution

Confirmed: chain order Deezer (10) → MusicBrainz (20, enrichment-only, 1 req/s + Retry-After pacing + negative cache preserved per-plugin) → Spotify (30) → iTunes (40). enrich_albums stays called from sync/import orchestration (core glue).

Claimed by: dev (this session). Resolved: user confirmed 2026-08-29.
