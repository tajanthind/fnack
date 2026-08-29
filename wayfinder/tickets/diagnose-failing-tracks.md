# Diagnose the 180 failing tracks and pick the highest-leverage fixes

wayfinder:research

## Question

The fnack DB at `/home/tajanthind/fnack-info/fnack.db` shows 180 failed tracks.
Breakdown: 117 = SoundCloud search junk (playlist results / DjPunjab rips
rejected by the verifier), 24 = verifier tag-mismatch rejections (wrong song
downloaded), 4 = SpotiFLAC "track not found", 2 = SpotiFLAC 429, remainder =
miscellaneous SoundCloud/YouTube errors.

Which changes raise the success rate the most, in what order?

Candidates to evaluate against the evidence (logs + DB):
- Drop or reshape the scsearch fallback (it produces junk for Punjabi tracks
  and burns time per failure).
- Refresh YouTube cookies (cookies.txt present, 14 cookies, stale?) and/or
  VPN-before-downloads.
- Enrich candidate generation via MusicBrainz / YouTube Topic channels.
- AcoustID fingerprint verification to catch wrong songs earlier / retry with
  a different candidate.
- Anything in the verifier or retry logic that wastes attempts.

Deliver: a prioritized, evidence-backed recommendation (top 3 fixes with
expected impact), plus the data behind it (failure clusters by artist/album).

Write findings to `wayfinder/research/diagnose-failing-tracks.md`.
