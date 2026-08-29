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

- [Diagnose the 180 failing tracks and pick the highest-leverage fixes](tickets/diagnose-failing-tracks.md):
  66% of failures are the SoundCloud search fallback producing junk — remove
  the `scsearch2:` targets; AcoustID verification + VPN/cookies cover the rest.
- [Explain and stop the phantom artists in Navidrome](tickets/phantom-artists.md):
  composer/producer credits in tags become Navidrome artist rows — extend
  `_tag_audio_file`'s strip list + normalizer check + a one-time cleanup
  script + full rescan.
- [Design the MusicBrainz catalogue integration](tickets/musicbrainz-integration.md):
  enrichment-only (`musicbrainz_service.py` after discography fetch);
  Deezer authoritative; regional negative cache; stale MB can only add;
  1 req/s, fail-soft.
- [Design optional AcoustID fingerprinting](tickets/acoustid-fingerprinting.md):
  optional key (`acoustid_api_key`); fpcalc subprocess, no new pip deps;
  verify-when-unsure (0.8 gate) + manual identify; regional no-match silent.
- [Zero required authentication — audit and close gaps](tickets/zero-auth-audit.md):
  no gaps — all human-facing surfaces unauthenticated; API key optional/M2M.
- [Regional-artist catalogue fallback strategy](tickets/regional-artist-fallback.md):
  keyless-disabled AcoustID by default; remove SoundCloud search fallback;
  no-match changes nothing; identification auto-applies, mismatches get a
  caution mark + "what it matched to", user keeps/deletes.

## Not yet specified

- ~~How the maintenance/merge policy should change once MusicBrainz facts
  land~~ → resolved: enrichment-only, Deezer authoritative.
- ~~Whether AcoustID runs on every download or only when unsure~~ → resolved:
  only when the verifier is unsure / on manual identify; optional
  every-download setting deferred until the user judges performance.
- ~~Navidrome config vs tag-stripping~~ → resolved: tag-stripping in fnack is
  the fix; cleanup script + full rescan for existing rows.

## Out of scope

- Mirroring the full MusicBrainz catalogue (only light-touch lookup).
- Replacing or re-installing the user's Navidrome instance.
