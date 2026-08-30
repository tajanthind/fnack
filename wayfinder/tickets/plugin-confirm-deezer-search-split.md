# Confirm: Deezer interactive search stays core; batch enrichment becomes the plugin

wayfinder:grilling

## Question

HARNESS_BRIEF §2's "Confirm? = Yes" row: `deezer_service.py` has both a
batch/import-time enrichment path and an interactive search endpoint
(`/api/search-artist` in `app.py:208`, which calls `search_artist(q, limit=8)`
directly, synchronously, on a user-typing hot path — `static/app.js` fires it
on every keystroke).

Decision to confirm:
1. The interactive `/api/search-artist` route stays **core**, calling the
   bundled Deezer provider directly (bypassing the plugin chain), per the
   decision rule (rule 1 — latency-sensitive, synchronous, user-facing).
2. The `metadata_provider` plugin wraps only the **batch/import** enrichment
   path (`get_artist_discography` used by sync/import), and if it shares a
   function with the interactive path, core and the plugin call the same
   underlying function rather than duplicating logic.

Also confirm: `spotify_service.py`'s search is used for URL resolution in the
download pipeline (ISRC → Spotify URL), not as a user-facing search box — so
it's a plain `metadata_provider` plugin, not core.

## Resolution

Confirmed: interactive /api/search-artist stays core, calling the bundled Deezer provider directly; metadata_provider plugin wraps only the batch/import enrichment path, sharing the same underlying function (no duplication). spotify_service is a plain metadata_provider plugin (pipeline URL resolution, not user-facing search).

Claimed by: dev (this session). Resolved: user confirmed 2026-08-29.
