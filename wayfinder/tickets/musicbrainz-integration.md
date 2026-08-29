# Design the MusicBrainz catalogue integration

wayfinder:research

## Question

Add MusicBrainz as a catalogue source to clean up / verify discographies of
major artists. Constraints from the user:
- It "doesn't usually have regional artists" (the library is mostly Punjabi:
  Happy Raikoti, Arjan Dhillon, Gur Sidhu…) — the flow must degrade
  gracefully to the current Deezer-first path.
- "For some artists it doesn't have updated discography" — stale data must be
  handled (never let a stale MusicBrainz result remove correct Deezer data).
- Zero required authentication.

Research and decide:
1. MusicBrainz API shape: release-group/release lookup, artist search by name,
   recording lookup, rate limits (1 req/s etiquette), cover-art archive —
   which endpoints matter for verifying/cleaning a discography.
2. How to detect "regional artist not in MusicBrainz" vs "artist present but
   stale" and what the fallback policy should be (Deezer stays authoritative;
   MusicBrainz only adds release-group IDs / canonical names when a confident
   match exists).
3. Where the lookup hooks in: at artist sync (cross-check Deezer albums
   against MusicBrainz release groups), at download verification, or both?
4. Caching to respect the rate limit.

Deliver: a concrete integration design (endpoints, matching thresholds,
reconciliation rules, caching, failure handling).

Write findings to `wayfinder/research/musicbrainz-integration.md`.
