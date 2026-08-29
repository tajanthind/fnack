# Explain and stop the phantom artists in Navidrome — research findings

**Source**: `/home/tajanthind/fnack-info/navidrome-config/navidrome.db` +
`/home/tajanthind/fnack-info/fnack.db` + a scan of actual audio files.
**Status**: research (no code changed).

## What is happening

Navidrome lists **969 artists**; fnack manages ~36. **889 artist rows have
zero album rows** — they are songwriter / producer / featured-artist credits:
"Soft Piano Music" (21 rows), "The Love Allstars" (20), "Jon Bellion" (4),
"Jason Evigan" (4), "Nick Mira" (1), plus long comma-joined writer lists
("Carter Lang, Dylan Wiggins, Justin Bieber, Eddie Benjamin…").

## Where they come from (evidence)

1. Navidrome's `media_file_artists` table (12,692 rows) has a **`role`**
   column, and the phantom names appear there with composer/featured roles.
2. `media_file.participants` JSON shows roles beyond `artist`/`albumartist`,
   e.g. `"composer":[...]` — Navidrome 0.63 parses **composer / producer /
   writer credits from embedded tags** into participant artist rows, and those
   rows surface in the Artists list.
3. Files fnack itself re-tagged (verified on disk: Gur Sidhu `.flac`/`.opus`)
   contain only `albumartist` + `artist` — `_tag_audio_file` already strips
   most credit tags **except composer/writer/producer/lyricist-style fields**.
   The phantom credits ride in on files downloaded by SpotiFLAC/yt-dlp and not
   yet normalized, via tags such as `TCOM`, `TEXT`, `composer`,
   `musiciancredits`, `producer`, `TXXX:composer`, MP4 `©wrt`, etc.

## Fixes

1. **fnack `_tag_audio_file`: strip credit/participant tags in every format**
   (FLAC vorbis: `composer`, `writer`, `lyricist`, `producer`, `arranger`,
   `performer`, `musiciancredits`; MP3: `TCOM`, `TEXT`, `TXXX:*`; MP4:
   `©wrt` + `----:com.apple.iTunes:*`; Ogg: same vorbis keys). Add them to the
   existing strip set so retagged files carry only `artist`/`albumartist`.
   Extend the metadata normalizer's "needs retag" check to include these keys,
   so the boot/6h maintenance pass cleans the whole existing library.
2. **Navidrome config**: no config file exists (env-only). Set
   `ShowTrackArtist=false` (default) — verify via docs; the dominant fix is
   the tag stripping, because the artist rows come from *participant roles*,
   not the track-artist toggle.
3. **One-time cleanup** (after the retag pass): delete `media_file_artists`
   rows whose role is not `artist`/`albumartist` (Navidrome regenerates them
   on rescan), then delete `artist` rows that have zero primary `media_file`
   rows and zero `album` rows, then trigger a Navidrome **full rescan** so the
   scanner rebuilds participants from the now-clean tags.

## Recommendation

- Ship the `_tag_audio_file` strip-list extension + normalizer check (prevents
  new phantoms and heals the library on the existing 6h/restart maintenance).
- Ship a `scripts/clean_navidrome_artists.py` that does the one-time cleanup
  against `navidrome.db` (same pattern as `fix_navidrome_splits.py`), run
  after the first retag pass, followed by a Navidrome full rescan.
