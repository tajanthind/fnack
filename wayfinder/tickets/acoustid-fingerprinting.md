# Design optional AcoustID fingerprinting

wayfinder:research

## Question

Add OPTIONAL AcoustID fingerprinting:
- Verify a downloaded song is really the expected track (the verifier already
  checks duration + tags; fingerprinting is stronger).
- Identify unknown/regional songs where tags are empty or wrong ("for those
  we can probably still lookup song fingerprint and see if something else
  shows up").
- It "doesn't have most of the regional artists" — a fingerprint lookup that
  finds nothing must not block the download; it's a bonus signal.

Research and decide:
1. AcoustID API: client API key requirement (free registration) — how does
   this stay OPTIONAL so zero auth is still required by nothing? (No key =
   fingerprinting disabled, everything else works.)
2. chromaprint/fpcalc availability: does `chromaprint`/`fpcalc` install in
   the python:3.11-slim image (apt package `libchromaprint-tools`)? Cost of
   running fpcalc per file; whether to fingerprint every download or only on
   verifier failure / manual lookup.
3. Lookup flow: fpcalc -> AcoustID API -> MusicBrainz recording; confidence
   thresholds; what fnack does with the result (confirm the track, surface a
   suggestion for manual match, retry with the identified candidate).
4. The unknown-song identification flow: given an audio file with no/weak
   tags, fingerprint -> AcoustID -> MusicBrainz -> candidate -> optional
   download.

Deliver: the optional-fingerprint design (key handling, fpcalc integration,
verification + identification flows, thresholds, fallbacks).

Write findings to `wayfinder/research/acoustid-fingerprinting.md`.
