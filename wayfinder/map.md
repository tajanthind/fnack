# fnack: reliable zero-auth downloads + clean catalogue

**Tracker**: local-markdown. Map = this file. Tickets = `tickets/*.md`, one per
decision, each carrying a `wayfinder:<type>` label line. Research findings =
`research/<ticket-name>.md`. Blocking is expressed in the ticket body
(`Blocked by:` line) since markdown has no native dependency graph.

## Destination

fnack downloads the *right* song for every track (failures near zero), the
Navidrome library shows only the artists the user manages (no songwriter /
phantom artists), with **zero required authentication**, MusicBrainz catalogue
enrichment that cleans up major artists' discographies and degrades gracefully
for regional artists and stale MusicBrainz data, and **optional** AcoustID
fingerprinting that confirms a download is the right song and can identify
unknown/regional tracks. The whole thing works reliably.

## Notes

- Domain: self-hosted music downloader (`Flask + gevent + SQLite WAL`).
  Downloads: SpotiFLAC (Tidal/Qobuz/Deezer/SoundCloud, zero-auth) →
  yt-dlp fallback → strict verifier (duration + embedded-tag containment;
  zero mismatched songs allowed).
- Fresh ground truth at `/home/tajanthind/fnack-info` (fnack + Navidrome
  logs, `fnack.db`, `navidrome.db`): 180 failed tracks, 889/969 Navidrome
  artists have no album (songwriter/featured credits in tags).
- Library is mostly Punjabi/regional (Happy Raikoti, Arjan Dhillon, Gur
  Sidhu…): MusicBrainz and AcoustID coverage is thin there — regional
  fallback is a first-class requirement, not an edge case.
- Standing preferences: zero required auth; no second container; runs in
  the same repo (`/home/tajanthind/fnack 2`), deploys via
  `docker compose up -d --build` (image `fnack:latest`).
- Every change ships as a tagged release (v0.2.x) and is pushed to
  `github.com/tajanthind/fnack` after approval.

## Decisions so far

<!-- one line per closed ticket; zoom the linked ticket for the detail -->

## Not yet specified

- How the maintenance/merge policy should change once MusicBrainz facts land
  (e.g., canonical title casing, year reconciliation between Deezer and
  MusicBrainz release groups).
- Whether AcoustID fingerprinting runs on every download or only when the
  verifier is unsure / the track failed (fpcalc CPU cost per file).
- Navidrome-side config change vs tag-stripping in fnack for hiding
  songwriter artists — which is visible to the user, and whether both are
  needed.

## Out of scope

- Mirroring the full MusicBrainz catalogue (only light-touch lookup).
- Replacing or re-installing the user's Navidrome instance.
