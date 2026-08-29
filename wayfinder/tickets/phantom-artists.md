# Explain and stop the phantom artists in Navidrome

wayfinder:research

## Question

Navidrome lists 969 artists while fnack manages ~36. 889 Navidrome artist
rows have zero album rows — they are songwriter/producer/featured-artist
credits (e.g. "Carter Lang, Dylan Wiggins, Justin Bieber, Eddie Benjamin…",
"Nick Mira, Jarad Higgins, Jeffrey Williams, Martin Puschel", "Soft Piano
Music") that Navidrome promotes into its Artists list.

Establish, with evidence:
1. Which embedded tag fields create these artist rows (songwriter /
   producer / `ARTISTS` / `musiciancredits` / MusicBrainz artist IDs) and
   whether they come from the downloaders (SpotiFLAC/yt-dlp) or from fnack's
   own tagging.
2. Which Navidrome config options hide non-album artists (e.g.
   `ShowTrackArtist`, participant handling, external enrichment) — check the
   Navidrome docs and the user's config (env-only, no config file found).
3. What fnack should do so new downloads never create them (tag stripping?)
   and how to clean the ~889 existing rows safely (delete empty artist rows?
   Navidrome rescan?).

Deliver: the tag/config fix recommendation and the cleanup procedure.

Write findings to `wayfinder/research/phantom-artists.md`.
