# Research: scale to millions of songs — counters, pagination, search

wayfinder:research

## Resolution

RESOLVED (research) — findings written to `wayfinder/research/scale-to-millions.md` (496 lines). Key verdicts: (1) denormalize Artist counters (total_albums/total_tracks/downloaded_tracks) with 42 write-points catalogued (queue 2, app 10, import 3, metadata merge moves, watcher 1, reverify 1) and backfill; response keys unchanged. (2) Paginate /api/artists (offset+limit now, keyset later, frontend switch ~2k artists) + cap /api/queue active at 500. (3) FTS5 DEFERRED — no user-facing LIKE scans exist; only bounded dedup query queue_service.py:630. (4) Plugin health bookkeeping must stay commit-free (scaffold call_safe already is); flush piggybacked on queue commits. (5) SQLite stays; already-solid list unchanged. Implement in Phase 1.

## Question

PHASE1_BUNDLED_PLUGINS_BRIEF §5. Before touching query patterns (items 1–3),
document the current query shape and the proposed denormalization/pagination/
FTS change, then resolve into the map.

Research (read-only, no code changes):
1. **Per-artist counters**: `/api/artists` (app.py:221) recomputes
   `total_albums`/`total_tracks`/`downloaded_tracks` via two full `GROUP BY`
   scans on every request. Document the exact queries, index coverage
   (models.py hot-column indexes), and the proposed denormalized columns on
   `Artist` (`total_albums`, `total_tracks`, `downloaded_tracks`) with the
   incremental update points (everywhere `Track.is_downloaded`/`status`
   changes, or an Album/Track is added/removed — plus the new
   `track.after_download`/`track.verified` event hooks as update points).
2. **Pagination**: `/api/artists` returns the full artist table serialized
   client-side; index.html + static/app.js render the whole grid. Propose
   server-side offset/cursor pagination + frontend paging/virtualization
   threshold (~few thousand artists). Check for any other full-listing
   endpoints (e.g. `/api/queue`, album/track lists in artist.html) that need
   the same treatment.
3. **Full-text search**: grep `metadata_service.py` + app.py search routes for
   `LIKE`/`ilike` substring scans on Track.title/Album.name/Artist.name; if
   found, propose a SQLite FTS5 virtual table kept in sync at the same write
   points as the counters.
4. **Plugin-manager write-lock discipline**: confirm the plugin health-log /
   auto-disable bookkeeping must not issue its own `db.session.commit()` per
   hook call (SQLite single-writer) — batch or piggyback on existing commit
   points in queue_service.
5. Confirm SQLite stays (documented non-goal: no Postgres) and the queue's
   1.5s single-job poll is fine today (don't build batched claiming now).

Deliver: current-shape notes + concrete proposed changes, written to
`wayfinder/research/scale-to-millions.md`.
