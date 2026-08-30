# Decide: orchestrators (import/metadata), verifier, lidarr, watcher — core vs plugin

wayfinder:grilling

## Question

PHASE1_BUNDLED_PLUGINS_BRIEF §2 "Flag, don't migrate yet — ask me first":

1. **`services/import_service.py` + `services/metadata_service.py`** — audit:
   both are orchestrators. `import_service` scans folders, calls Deezer
   search/discography, maps local files, writes `Album`/`Artist`/`Track` rows,
   and keeps scan/search caches (30s/600s TTL). `metadata_service` normalizes
   tags, merges duplicate albums, backfills artwork, and imports
   `_sanitize`/`_tag_audio_file` from `queue_service`. Proposal: **core glue**
   (rule 2/3) — they become the code that *calls* the plugin chains, not
   plugins themselves.
2. **`services/verifier_service.py`** — the map's whole destination is zero
   mismatched songs via strict verification. Proposal: **core, safety-critical**
   (rule 2), not disable-able via a plugin toggle. It stays a plain core
   module; the queue calls it directly.
3. **`services/lidarr_service.py`** — audit: it's the SABnzbd download-client
   emulation + Newznab/Torznab indexer emulation (`/api/sabnzbd/*`,
   `/api/newznab`, `/api/torznab`, `/api/nzb/<type>/<id>` routes) and it builds
   Artist/Album/Track + DownloadJob rows from NZB grabs. It's a **source of
   artists/albums to monitor** (matches the proposed `library_source` type) AND
   a route-registering server extension. Question: new `library_source` plugin
   type, fit inside `scan_trigger`, or `server_extension`?
4. **`services/watcher_service.py`** — audit: real-time folder watcher
   (watchdog `Observer`) syncing Track rows + Navidrome metadata on
   delete/rename/move. Question: `library_task` (scheduled) or its own
   event-driven type? Note: it starts at boot and runs continuously — more
   event-driven than cron-like.

## Resolution

Confirmed: import_service + metadata_service are CORE orchestrators (call the chains, not plugins); verifier_service is CORE safety-critical, not disable-able. Lidarr → new library_source plugin type (Phase 2+ design); watcher → own event-driven type or stays core (Phase 2+ decision).

Claimed by: dev (this session). Resolved: user confirmed 2026-08-29.
