# Decide: spotdl plugin form + priority

wayfinder:grilling

## Question

PHASE1_BUNDLED_PLUGINS_BRIEF §2 lists `services/spotdl_service.py` as a
`downloader` plugin with "Confirm relative priority vs. yt-dlp/SpotiFLAC with
me — not specified yet."

Audit finding: `services/spotdl_service.py` is a **thin compatibility shim** —
its only function `download_track_spotdl()` forwards 1:1 to
`download_track_ytdlp()` in `services/ytdlp_service.py` (same options, same
defaults; the file's own docstring says "Forwarding all requests to
ytdlp_service"). There is no independent spotdl behavior left to migrate.

Decision to confirm:
1. Treat spotdl as an **alias of the yt-dlp downloader plugin** — one
   `downloader` plugin (`id: fnack.ytdlp`) handling both entry points
   (`can_handle` matches the same inputs), rather than a separate plugin with
   its own priority slot.
2. If you'd rather keep a distinct `fnack.spotdl` plugin for visibility, it
   gets priority = yt-dlp's (e.g. 50) and wraps the same underlying function —
   no behavior change, just an extra manifest row.
3. Whether anything else in the repo still imports `spotdl_service` besides
   `queue_service.py` (to be confirmed by grep during Phase 1) — if the shim
   becomes dead code after the migration, propose deleting it (flag, don't
   silently remove).

## Resolution

Confirmed: spotdl is an alias of the yt-dlp downloader plugin — one plugin (fnack.ytdlp) handles both entry points; no separate spotdl plugin row. During Phase 1, grep for remaining spotdl_service imports; if the shim becomes dead code, flag (don't silently delete).

Claimed by: dev (this session). Resolved: user confirmed 2026-08-29.
