# Regional-artist catalogue fallback strategy

wayfinder:grilling

Blocked by: Design the MusicBrainz catalogue integration, Design optional
AcoustID fingerprinting

## Question

The library is dominated by Punjabi/regional artists that MusicBrainz and
AcoustID mostly don't cover. Decide the fallback strategy so the
catalogue/discography flow never depends on either service.

## Resolution (decided with the user)

- **AcoustID key**: strictly optional. Without a key the feature is disabled
  (no "Identify this file" action, no fingerprint lookups) and everything
  else works unchanged. The user adds a free acoustid.org key later if they
  want; ship it as an optional Settings field ("keyless disabled" by
  default). Treat AcoustID as an evolving capability: ship it optional, let
  the user judge performance, revisit reliance later.
- **SoundCloud search fallback**: REMOVE the `scsearch2:` targets from the
  yt-dlp fallback (approved). Direct SoundCloud URL downloads stay.
- **No MusicBrainz / no AcoustID match**: must change NOTHING — Deezer +
  yt-dlp remain the pipeline exactly as today; no error, no retry, no UI
  noise.
- **AcoustID identification is auto-applied**: fingerprint → AcoustID →
  candidate. If the candidate matches the expected track → auto-apply
  (retag / keep). If the candidate is something DIFFERENT → do NOT silently
  delete: flag the song with a **caution mark** in the UI, show the user
  what it actually matched to (recording/artist), and let them decide
  keep or delete. This supersedes silent mismatch deletion for
  AcoustID-confirmed mismatches.
- **Verifier override**: AcoustID confirmation at the strict gate (≥0.8 +
  cross-checks) auto-accepts "right file, wrong tags" and lets finalize
  retag.
- **MusicBrainz**: enrichment-only; Deezer authoritative; regional artists
  never probed (negative cache); stale data can only add, never remove.
