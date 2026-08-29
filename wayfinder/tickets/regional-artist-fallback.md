# Regional-artist catalogue fallback strategy

wayfinder:grilling

Blocked by: Design the MusicBrainz catalogue integration, Design optional
AcoustID fingerprinting

## Question

The library is dominated by Punjabi/regional artists that MusicBrainz and
AcoustID mostly don't cover. Decide the fallback strategy so the
catalogue/discography flow never depends on either service:

- When MusicBrainz has no match for an artist or release, what exactly
  happens? (Proposed default: Deezer remains authoritative; MusicBrainz only
  enriches confident matches; regional artists flow exactly as today.)
- When AcoustID has no fingerprint match, what does the user see?
  (Proposed: no change — the verifier's duration/tag checks remain, and the
  manual-match screen can offer an optional "identify this file" action that
  uses AcoustID when a key is configured.)
- When a regional artist DOES show up in AcoustID (some regional tracks are
  fingerprinted), how should the suggestion surface without auto-replacing
  the user's choice?

This ticket is a live discussion with the user; the agent must not answer
for them. Bring the two research findings (MusicBrainz + AcoustID) to the
conversation and resolve the fallback behavior for each case.
