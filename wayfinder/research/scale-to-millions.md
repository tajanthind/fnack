# Research: scale to millions of songs — counters, pagination, search

Label: `wayfinder:research`
Ticket: `wayfinder/tickets/scale-to-millions.md` — PHASE1_BUNDLED_PLUGINS_BRIEF §5.
Status: **resolved for map** (read-only research; no code changed).

Line numbers below are against the repo tip at the time of writing
(`app.py` 1884 lines, `models.py` 163, `queue_service.py` 1370,
`metadata_service.py` 638, `import_service.py` 515, `watcher_service.py` 263,
`static/app.js` 1633, `templates/index.html` 254).

---

## 1. Current shape

### 1.1 `/api/artists` — two full GROUP BY scans + full-table serialize, on every request

`app.py:221-264` (`api_artists`). Three queries run on **every** page load and on
every `artist_updated`/`artist_added`/`artist_synced` socket event (the frontend
refetches both endpoints together, `static/app.js:296-299`, with a 2s debounce
reload on `artist_updated`, `app.js:1551-1559`):

1. `app.py:224-228` — album count per artist:
   ```sql
   SELECT albums.artist_id, count(albums.id) FROM albums GROUP BY albums.artist_id
   ```
   Covered by `idx_albums_artist_id` (models.py:51 + app.py:1771) — an index-only
   scan, but still O(total_albums) per request.

2. `app.py:230-240` — track + downloaded counts per artist:
   ```sql
   SELECT albums.artist_id,
          count(tracks.id),
          sum(CASE WHEN tracks.is_downloaded = 1 THEN 1 ELSE 0 END)
   FROM tracks JOIN albums ON tracks.album_id = albums.id
   GROUP BY albums.artist_id
   ```
   O(total_tracks) per request. **Attribution semantics: tracks are grouped by
   `albums.artist_id`, NOT `tracks.artist_id`** — a track counts toward the
   artist its *album* belongs to. Any denormalized replacement must preserve
   this exact semantics (see §2.4).

3. `app.py:242` — `Artist.query.order_by(Artist.name).all()` — full artist table
   materialized, then serialized with no pagination (`app.py:243-264`).

Payload: one JSON object per artist with 13 keys (`id, name, image_url,
monitored, auto_download, source, sync_status, sync_error, last_synced_at,
total_albums, total_tracks, downloaded_tracks, percent_downloaded`); `percent_downloaded`
computed at `app.py:262`.

### 1.2 `/api/stats` — single global aggregate scan per request

`app.py:267-304` (`api_stats`). One pass over the whole `tracks` table
(`app.py:273-278`):
```sql
SELECT count(tracks.id),
       sum(CASE WHEN tracks.is_downloaded = 1 THEN 1 ELSE 0 END),
       sum(CASE WHEN (tracks.status IN ('failed','error'))
                 OR (tracks.is_downloaded = 0 AND tracks.error_message IS NOT NULL AND tracks.error_message != '')
            THEN 1 ELSE 0 END),
       sum(CASE WHEN tracks.is_downloaded = 1 THEN tracks.size_bytes ELSE 0 END)
FROM tracks
```
plus `Artist.query.count()` and `filter_by(monitored=True).count()` (`app.py:270-271`).
This is a full-table scan on every page load — O(n) but a single sequential scan
(no joins, no sort); acceptable at millions of rows on SQLite WAL (a ~1M-row
scan is tens of ms), and it is fetched in parallel with `/api/artists` on every
load (`app.js:296-299`). Flag only: it could later be derived cheaply from the
denormalized counters (§2) for `total_artists`/`downloaded_tracks`; the
`failed_tracks`/`size_bytes` terms would still need the scan. **Not a blocker —
leave as-is for now.**

### 1.3 Index coverage on hot columns (models.py + app.py boot block)

Already indexed (models.py `index=True` + idempotent `CREATE INDEX IF NOT EXISTS`
at app.py:1770-1777):

| Table | Indexed columns | models.py |
|---|---|---|
| Artist | `name` (14), `monitored` (17), `sync_status` (21), `spotify_id` unique (13) | + app.py:1770 |
| Album | `artist_id` (51), `name` (52), `deezer_id` (55), `record_type` (56), `is_downloaded` (58), `monitored` (59), `mb_release_group_id` (64) | + app.py:1771 |
| Track | `album_id` (82), `artist_id` (83), `title` (84), `isrc` (87), `deezer_id` (88), `status` (96), `monitored` (98), `is_downloaded` (102), `caution` (108) | + app.py:1772-1776 |
| DownloadJob | `track_id` (118), `album_id` (119), `artist_id` (120), `status` (128), `created_at` (135), `updated_at` (140) | + app.py:1777 |

All hot columns the GROUP BY scans touch (`album.artist_id`, `track.album_id`,
`track.is_downloaded`, `track.status`) are covered by single-column indexes.
There is no composite index; none is needed for the current aggregate shapes.

### 1.4 LIKE / ilike substring scans — findings

Grep across all `*.py` for `.like(`, `ilike`, `LIKE ` and `%…%` query patterns:

- **`services/queue_service.py:630`** — `Track.title.ilike(f"%{track_title[:60]}%")`
  inside `_process_track_job`'s cross-artist/featuring dedup step
  (`queue_service.py:621-640`). This is the **only** SQL `LIKE` in the codebase.
  It is a *bounded candidate-narrowing* query, not user-facing search: it is
  pre-filtered by `Track.is_downloaded == True`, `local_path IS NOT NULL`/`!= ''`,
  `Track.id != track_id`, then `.limit(50)` (`queue_service.py:625-631`), and the
  LIKE match is re-checked in Python against normalized title + duration
  (`queue_service.py:632-640`). Runs once per download attempt. Title index
  (`models.py:84`) cannot serve the leading-`%` pattern, so it is a scan over
  downloaded tracks — at millions of tracks this is the one query an FTS5 index
  could speed up, but it is already capped at 50 rows and gated behind
  `is_downloaded` first, so it is **not** the hot path.
- **`services/metadata_service.py`** — no `.like(`/`ilike` in any query. The
  only name matching is exact after SQL-side normalization:
  `sa.func.lower(sa.func.trim(Album.name)) == norm_name` (`metadata_service.py:226-239`),
  plus in-Python `SequenceMatcher` fuzzy comparison on normalized names
  (`metadata_service.py:267-270, 327-330`). These run only in the detached
  maintenance subprocess (`scripts/run_maintenance.py`), not per request.
- **`app.py` search routes** — the only search route is
  `/api/search-artist` (`app.py:208-218`), which calls the **external** Deezer
  API (`services/deezer_service.py:52` `search_artist`) for artist *onboarding*;
  it never queries the local DB. There is **no in-library search endpoint**.
- All other `%` grep hits are Python `%`-format log strings, not SQL.

**Verdict for §4:** no LIKE scans in the search paths (metadata_service.py, app.py
search routes) — **FTS5 deferred/optional**; the single bounded `ilike` at
`queue_service.py:630` does not justify an FTS5 table now (§4).

### 1.5 Full-listing endpoints lacking pagination (complete inventory)

| Endpoint | Shape | Lines | Needs work? |
|---|---|---|---|
| `GET /api/artists` | full artist table serialized, no paging | app.py:221-264 | **YES — primary target** |
| `GET /api/queue` — `active` | all `status IN ('downloading','queued')` jobs, unbounded | app.py:1124 | **YES** — a bulk import / retry-all can leave thousands queued; render loop `app.js:1420-1450` builds all of them |
| `GET /api/queue` — `history` | `.limit(50)` — already bounded | app.py:1125 | no (keep) |
| `GET /api/artist/<id>` | full discography: all albums + **all tracks** in one payload | app.py:544-636 | **Flag** — per-artist; heavy only for 100+ album artists (artist.html album/track lists are client-rendered from this payload, `app.js:412-509`). Defer; revisit with per-album lazy fetch if a single artist page becomes slow |
| `GET /api/stats` | single aggregate, not a listing | app.py:267-304 | no (see §1.2) |
| Scheduler: `Artist.query.filter_by(monitored=True).all()` | full table, background loop every 6h | app.py:1708 | no (background, not user-facing) |
| Import scan: `Artist.query.all()` | full table, folder scan | import_service.py:189 | no (background, TTL-cached) |

Frontend rendering evidence (full-list, no paging/virtualization):
- `static/app.js:291-311` `loadDashboardArtists()` — fetches both endpoints,
  stores the whole array in `_libraryArtists`.
- `static/app.js:313-363` `renderDashboardArtists()` — builds one HTML string for
  **all** artists into `#artistsDashboardGrid` (`index.html:142`).
- `static/app.js:274-289` `applyLibraryFilter()`/`initLibraryFilter()` — filter is
  a client-side substring over the full in-memory array.
- No `IntersectionObserver`, no paging, no "load more" anywhere in app.js.

---

## 2. Proposed denormalization — per-artist counters

### 2.1 New columns on `Artist` (models.py, `Artist` class ~line 9-44)

```python
total_albums       = db.Column(db.Integer, default=0, nullable=False)
total_tracks       = db.Column(db.Integer, default=0, nullable=False)
downloaded_tracks  = db.Column(db.Integer, default=0, nullable=False)
```

Defaults 0 so a new `Artist` row (e.g. `app.py:508-524`) needs no special
handling. Add to the boot migration block (`app.py:1765-1815`, same pattern as
the existing `ALTER TABLE albums ADD COLUMN monitored…` at app.py:1780-1783):

```sql
ALTER TABLE artists ADD COLUMN total_albums INTEGER DEFAULT 0;
ALTER TABLE artists ADD COLUMN total_tracks INTEGER DEFAULT 0;
ALTER TABLE artists ADD COLUMN downloaded_tracks INTEGER DEFAULT 0;
```

No indexes needed — these are read whole-row, never filtered/joined on.

### 2.2 Boot-time backfill (one-shot, idempotent)

After the `ALTER TABLE`s, recompute once from the existing aggregates (the exact
queries from §1.1) so existing libraries are correct without waiting for the
next write:

```sql
UPDATE artists SET total_albums = COALESCE((
  SELECT count(*) FROM albums WHERE albums.artist_id = artists.id), 0);
UPDATE artists SET total_tracks = COALESCE((
  SELECT count(*) FROM tracks JOIN albums ON tracks.album_id = albums.id
   WHERE albums.artist_id = artists.id), 0);
UPDATE artists SET downloaded_tracks = COALESCE((
  SELECT count(*) FROM tracks JOIN albums ON tracks.album_id = albums.id
   WHERE albums.artist_id = artists.id AND tracks.is_downloaded = 1), 0);
```

(Or reuse the two GROUP BY queries in Python inside the same `with
db.engine.connect()` block.) Also expose `recount_artist_counters()` as a
library_task-friendly maintenance entry so a drift can be self-healed by the
weekly maintenance pass (`scripts/run_maintenance.py`) without a redeploy.

### 2.3 Exhaustive write points — every place the counters must change

Central rule: a track's contribution belongs to **`track.album.artist_id`** (the
semantics of the current GROUP BY at app.py:232-239), and the deltas are:

- `total_tracks` += 1 on Track **insert**, −1 on Track **delete**
- `total_albums` += 1 on Album **insert**, −1 on Album **delete** (cascade deletes
  of its tracks also decrement `total_tracks`/`downloaded_tracks`)
- `downloaded_tracks` += 1 on `is_downloaded` **False→True**, −1 on **True→False**
  (guard the delta: re-download of an already-downloaded track must not double-count)
- moving a Track to another **album** (or an Album to another artist) moves the
  track's contribution between artists

A single helper `_bump_artist(artist_id, dt_total=0, dt_downloaded=0)` /
`_bump_album_counts(album)` used at every point below keeps the arithmetic in one
place.

**services/queue_service.py** (all `db.session.commit()` lines verified):
| # | Function | Line(s) | Delta |
|---|---|---|---|
| 1 | `queue_track` — reactivate failed job: `track.status="queued"` | 363-368 | none (no is_downloaded change) |
| 2 | `queue_track` — new job: `track.status="queued"`, commit | 387-390 | none |
| 3 | `_process_track_job` — job w/o track: `job.status="failed"` | 497-499 | none |
| 4 | `_process_track_job` — ISRC/genre auto-resolve commit | 551 | none (metadata only) |
| 5 | **`_process_track_job` success: `track_rec.is_downloaded=True` (821), `status="completed"` (822), caution set (835-838), commit 854** | 820-854 | **+1 downloaded_tracks** (False→True; no total_tracks change — track pre-existed). Emit `track.after_download` + `track.verified` here (INTEGRATION.md §5) |
| 6 | `_process_track_job` failure: `track_rec.status="failed"`, commit | 892-900 | none (is_downloaded stays False) |
| 7 | `_process_track_job` exception: `track_rec.status="failed"`, commit | 921-924 | none |
| 8 | `_handle_cancellation`: `track.status="missing"`, commit | 949-952 | none (only queued/downloading jobs are cancellable) |
| 9 | `start_queue_worker` stale reconcile: status→"queued", commit | 975-983 | none |
| 10 | worker claim: status→"downloading", commit | 1017-1023 | none |
| 11 | `download_manual_match_track` — status="downloading", commit | 1084-1087 | none |
| 12 | `download_manual_match_track` — failure paths, commits | 1218-1221, 1245-1248 | none |
| 13 | **`download_manual_match_track` success: `is_downloaded=True` (1291), `status="completed"` (1292), commit 1318** | 1290-1318 | **+1 downloaded_tracks** (guard False→True — manual re-download of an already-downloaded track) |
| 14 | `download_manual_match_track` — exception, commit | 1356-1362 | none |

`_verify_or_rescue` (queue_service.py:432-484) writes nothing itself; it only
returns the AcoustID verdict/`caution_info` that the caller persists at #5.

**app.py**:
| # | Function | Line(s) | Delta |
|---|---|---|---|
| 15 | `api_add_artist` — Artist created | 508-524 | new row, counters 0/0/0 (defaults) |
| 16 | `_sync_artist_discography_background` — Album/Track inserts | 397-449 | **+1 total_albums per album, +n total_tracks per album** (commit 468) |
| 17 | `_sync_artist_discography_background` — prune stale albums + tracks (deletes) | 453-463 | **−1 total_albums, −n total_tracks, −dl downloaded_tracks** (commit 468) |
| 18 | `api_delete_artist` — `db.session.delete(artist)` cascades albums/tracks | 648 | artist row gone; no other artist affected |
| 19 | `api_delete_album` (**dead duplicate route, see note**) — deletes album row | 666-688 | **−1 total_albums, −n total_tracks, −dl downloaded_tracks** |
| 20 | `api_album_delete` (effective handler for `DELETE /api/album/<id>`) — resets every track to missing | 804-833 | **−n downloaded_tracks** (tracks kept, `is_downloaded=False` at 822-823; commit 831) |
| 21 | `api_track_delete` — `is_downloaded=False` (860), `status="missing"` (861) | 845-875 | **−1 downloaded_tracks** (commit 874) |
| 22 | `api_track_cancel` — `status="missing"` | 924-934 | none |
| 23 | `api_track_identify` (AcoustID apply-candidate) — **`t.artist_id = artist.id` (986)**, caution cleared (989-990) | 957-1010 | **none to counters** — `artist_id` changes but `album_id` does not; counters follow `album.artist_id` (matches current GROUP BY). Only a tag/title change (commit 1008) |
| 24 | `api_track_caution` delete action — `is_downloaded=False` (1068), `status="missing"` (1069) | 1035-1080 | **−1 downloaded_tracks** (commit 1078) |
| 25 | `api_track_caution` keep action — **`t.artist_id = artist.id` (1089)** | 1082-1115 | none (same reasoning as #23; commit 1113) |
| 26 | `api_retry_all_failed` — `j.track.status="queued"` | 1151-1196 | none (commit 1194) |
| 27 | `api_job_retry` — `j.track.status="queued"` | 1205-1217 | none (commit 1216) |

> **Note (dead route):** `DELETE /api/album/<album_id>` is registered **twice** —
> app.py:666 (`api_delete_album`, deletes the row) and app.py:804
> (`api_album_delete`, resets tracks to missing). Werkzeug matches the last-added
> rule for the same path+method, so the 804 handler is the live one and 666 is
> dead code. Flag for cleanup in Phase 1; both are listed as write points so the
> counter hooks are correct whichever handler survives.

**services/import_service.py**:
| # | Function | Line(s) | Delta |
|---|---|---|---|
| 28 | `import_artist_folder` — Artist create (290-306), Album inserts (315-324), Track inserts (337-349), commit 375 | 233-375 | **+1 total_albums, +n total_tracks** per new album |
| 29 | `import_artist_folder` — local-file match: `is_downloaded=True` (432), `status="completed"` (433) | 430-440 | **+1 downloaded_tracks** (guarded by `not target_track.is_downloaded` at 430) |
| 30 | `import_artist_folder` — unmatched tracks added with `is_downloaded=True` (483-484) | 457-486 | **+1 total_tracks, +1 downloaded_tracks** per unmatched file, **+1 total_albums** for the "Unmatched Local Tracks" album (461-468) |
| 31 | `import_artist_folder` — final commit | 506 | all of the above |

**services/metadata_service.py** (maintenance subprocess — must update counters too):
| # | Function | Line(s) | Delta |
|---|---|---|---|
| 32 | `_merge_album_into` — track moved to canonical album: `track.album_id = canonical.id` (152) | 102-164 | **counters move between artists** only when cross-artist (see #33); same-artist merges are net-zero for the artist |
| 33 | `_merge_album_into` — **cross-artist move: `track.artist_id = canonical.artist_id` (153-154)** | 102-164 | **−1 total_tracks/−1 downloaded_tracks on source artist, +1/+1 on destination** (album move implies artist move in pass 3) |
| 34 | `_merge_album_into` — adoption: `existing.is_downloaded=True` (119), `status="completed"` (120) | 110-120 | **none** — the downloaded track is moved within the same artist's canonical album (net zero); guard anyway |
| 35 | `_merge_album_into` — dup-track delete (149), dup-album delete (163) | 148-163 | **−1 total_tracks, −dl downloaded_tracks, −1 total_albums** on the source artist (net zero in same-artist passes, real delta in cross-artist pass) |
| 36 | `_merge_duplicate_albums` — commit points (one per pass) | 217, 282, 348 | persists all of the above |

`normalize_album_tags` (metadata_service.py:478-638) only rewrites
`t.local_path`/`t.file_path` (526-527) — **no counter change** (commit 606).
`_backfill_album_artwork` (395-475) writes files only. `has_stray_credits`
(metadata_service.py:557-560) inspects file tags via mutagen, not SQL.

**services/watcher_service.py**:
| # | Function | Line(s) | Delta |
|---|---|---|---|
| 37 | `on_deleted` — replacement found: `is_downloaded=True` (149), `status="completed"` (148) | 140-153 | none (guard False→True only; commit 151) |
| 38 | **`on_deleted` — file deleted: `is_downloaded=False` (156), `status="missing"` (157)** | 155-169 | **−1 downloaded_tracks** (commit 169) |
| 39 | `on_moved` — path-only updates | 205-217 | none (commit 216) |

**services/lidarr_service.py**:
| # | Function | Line(s) | Delta |
|---|---|---|---|
| 40 | `handle_sabnzbd_api` delete mode — `j.status="cancelled"` | 104-106 | none |
| 41 | `handle_newznab_api` grab — `track.status="queued"` (251), `album.is_downloaded=False` (256) | 245-257 | none (album flag only; commit 257) |

**scripts/reverify_library.py** (manual maintenance — must update counters too):
| # | Function | Line(s) | Delta |
|---|---|---|---|
| 42 | mismatch flagged: **`t.is_downloaded=False` (98), `t.status="failed"` (99)** | 95-112 | **−1 downloaded_tracks** per flagged track; commits at checkpoint (every 25) and final (`db.session.commit()` in the loop + end of block) |

`scripts/clean_navidrome_artists.py` / `scripts/fix_navidrome_splits.py` touch the
Navidrome DB, not fnack tracks — out of scope. `scripts/run_maintenance.py` and
`scripts/normalize_album_tags.py` delegate to `metadata_service` (covered by
#32-36).

### 2.4 How the new event hooks become update points

INTEGRATION.md §5 places the additive emissions in `queue_service._process_track_job`
after a successful download and after verification:

```python
plugin_manager.event_bus.emit("track.after_download", track_id=track.id)
plugin_manager.event_bus.emit("track.verified",      track_id=track.id)
```

Recommended split of responsibility:

1. **Core-owned counter updates at the existing commit points** — the `_bump_artist`
   helper is called inline at the write points in §2.3 (#5, #13, #16-17, #19-21,
   #24, #28-31, #32-36, #38, #42) so the invariants are explicit, unit-testable,
   and not dependent on plugin wiring. The existing `db.session.commit()` at
   queue_service.py:854 is the natural single place for the `+1 downloaded_tracks`
   in the main download path.
2. **The same helper is also registered as the internal handler for
   `track.after_download` / `track.verified`** — so any Phase-2 plugin-chain
   download path that completes a track through `call_safe` (INTEGRATION.md §6)
   maintains counters without the core downloader having to know. The handler
   does the False→True guard against `track.is_downloaded` and updates
   `track.album.artist_id`'s counters. `track.verified` is a no-op for counters
   (verification never changes `is_downloaded` itself) — it exists so the
   hook fires in the verify path.
3. **Semantics preserved:** counters follow `album.artist_id` exactly like the
   current GROUP BY (app.py:232-239). Cases where only `track.artist_id` changes
   (#23, #25) must NOT touch counters — this is a deliberate, documented
   non-change so the artist grid stays identical.

### 2.5 `/api/artists` response shape stays identical

`api_artists` (app.py:221-264) is rewritten to read the three columns off the
`Artist` rows (one indexed `ORDER BY name` query + optional pagination, §3) and
keep computing `percent_downloaded` at app.py:262. **All 13 JSON keys and their
types are unchanged** — the frontend (`app.js:313-363`, `index.html`) renders
the same objects, so the counters change is invisible to it. `_libraryArtists`
and the client-side filter keep working untouched in non-paged mode.

---

## 3. Proposed pagination

### 3.1 `/api/artists` — server-side paging

- **API:** `GET /api/artists?limit=200&offset=0` (and `?q=` for the name filter,
  see below). Offset+limit is fine at the "few thousand artists" threshold; a
  **keyset cursor over `(name, id)`** (`?after_name=…&after_id=…`) is the
  recommended upgrade when the artist table itself grows into the tens of
  thousands — `ORDER BY name` is not unique (duplicate names exist, e.g.
  `acoustid:` synthetic artists at app.py:983/1086), so plain `OFFSET` degrades
  with depth and can skip/dup on concurrent inserts. Concretely:
  `WHERE (name, id) > (:after_name, :after_id) ORDER BY name, id LIMIT :limit`.
- **Envelope:** with paging params present, return
  `{"artists": [...], "total": <count>, "limit":…, "offset":…}`; without params,
  keep returning the bare array (backward compatible for the transition).
  `total` from `SELECT count(*) FROM artists` (cheap, index-only on PK).
- **Filter:** the client-side filter (`app.js:274-279`) moves server-side once
  paged: `q` → `WHERE name LIKE 'q%' OR name LIKE '%q%'` (artist count is small;
  prefix form uses `idx_artists_name`, app.py:1770). Keep the client filter as a
  secondary refinement of the currently-loaded page during the transition.
- **Counters:** `total_albums`/`total_tracks`/`downloaded_tracks` come from the
  denormalized columns (§2), so each page is one indexed query + one count —
  no GROUP BY scans, no N+1.

### 3.2 Frontend switch threshold + changes (index.html + static/app.js)

- **Threshold:** switch to paged fetch when `stats.total_artists` (or a
  `total` header from `/api/artists`) exceeds **~2,000 artists** (the brief's
  "roughly a few thousand"). Below that, keep today's full-list behavior
  (payload ~2,000 × ~200 B ≈ 400 KB is fine; zero frontend churn).
- **Mechanism (above threshold):** replace the single `loadDashboardArtists()`
  fetch (`app.js:291-311`) with paged fetch + infinite scroll / "Load more"
  button on `#artistsDashboardGrid` (`index.html:142`). Keep the debounced
  reload on socket events (`app.js:1551-1559`) but make it refresh **page 1
  only** and re-request pages already in view. The `libraryFilterInput`
  (`index.html:133`, `app.js:281-289`) becomes a debounced server-side query.
  Virtualization is optional and only needed if a single page is itself huge —
  paged fetch of 200-artist pages renders fast enough that virtualization can be
  skipped in Phase 1.
- **Stats bar** (`index.html:96-122`) keeps using `/api/stats`; unchanged.

### 3.3 Other endpoints needing the same treatment

1. **`/api/queue` active list (app.py:1124)** — bound it: `…ORDER BY
   DownloadJob.created_at DESC .limit(500)` (or paginate). Today a bulk import
   or "Retry All Failed" (`app.py:1151-1196`) can create thousands of queued
   rows; the queue page (`app.js:1420-1450`) renders all of them into DOM.
   History is already capped at 50 (`app.py:1125`) — keep.
2. **`/api/artist/<id>` (app.py:544-636)** — full discography payload. Defer to
   a follow-up: only heavy for 100+ album artists; if it ever matters, add
   per-album lazy track fetch in `renderAlbumSection`/`renderAlbumAccordionCard`
   (`app.js:494-509`) instead of shipping all tracks in the page payload.
3. `/api/stats`, scheduler scans, import scans — no change (§1.2, §1.5).

---

## 4. FTS5 proposal

**No LIKE scans found in the search paths** — `metadata_service.py` and the
`app.py` search route (`/api/search-artist`, app.py:208-218, which is external
Deezer lookup) contain **no** `LIKE`/`ilike` on `Track.title`/`Album.name`/
`Artist.name`. The only SQL `LIKE` in the codebase is the bounded, non-user-facing
dedup query at `queue_service.py:630` (`.limit(50)`, gated by `is_downloaded`,
§1.4).

**FTS5 deferred/optional.** Recommendation: do **not** build an FTS5 virtual
table in Phase 1. If/when an in-library search feature is added (not present
today), add `tracks_fts`/`albums_fts`/`artists_fts` with contentless or
external-content tables kept in sync via triggers at the same write points as
the counters (§2.3), and revisit the `queue_service.py:630` dedup query then.
Until a user-facing search exists, FTS5 is speculative complexity.

---

## 5. Plugin-manager write-lock discipline

**Where queue_service commits (confirmed, all 15):** queue_service.py:368, 390,
499, 551, 854, 900, 924, 952, 983, 1023, 1087, 1221, 1248, 1318, 1362.
The per-job write-lock cost today is: 1 claim commit (1023) + 1 terminal commit
(success 854 / failure 900 / exception 924 / cancelled 952) — i.e. **2 SQLite
write-lock acquisitions per job** (plus the stale-reconcile 983 at boot).

**Findings / recommendation:**
1. The Phase-0 scaffold's `PluginManager.call_safe()` (`plugins/manager.py`
   `_call_safe`) performs **no `db.session.commit()`** — health bookkeeping is
   in-memory (`consecutive_failures` counter + `logger.exception`), and
   auto-disable only toggles an in-memory `loaded.enabled` flag. That discipline
   is correct and must survive Phase 1.
2. If Phase 1 persists a health log / auto-disable state to the DB (a
   `PluginHealthLog` table + `InstalledPlugin.enabled=False`), it must **not**
   commit per hook call. SQLite is single-writer even in WAL; every extra commit
   adds a write-lock acquisition that scales with plugin count, not library size
   (brief §5.4).
3. **Concrete bookkeeping piggyback points (no per-hook commits):** the manager
   accumulates health-log rows in an in-memory buffer; `queue_service` calls
   `plugin_manager.flush_health_log()` immediately before its own existing
   commits at **queue_service.py:1023 (claim), 854 (success), 900 (failure),
   924 (exception), 952 (cancellation)** — one extra commit only when the buffer
   is non-empty, riding the job's own write lock. The same flush point applies
   in `import_service.py:506` and `metadata_service.py:217/282/348` for
   plugin activity triggered by those paths.
4. Event hook callbacks (`track.after_download`, `track.verified`, etc.) run
   inside `_process_track_job` between its own commits — the `event_bus` must
   remain commit-free; plugins get `PluginContext` which exposes no `db.session`
   (PLUGIN_ARCHITECTURE.md §6), so they cannot commit even if they wanted to.
   The counter helper from §2.4 is the one sanctioned DB touch from the event
   path, and it rides the job's existing commit.
5. **Do not build batched job claiming now** (brief §5.5): the 1.5s single-job
   poll (queue_service.py:1007-1029) is fine today; nothing in this change makes
   batched claiming harder later.

---

## 6. Already solid — don't touch (PHASE1 §5 checklist)

- **SQLite tuning (app.py:107-122):** WAL, `synchronous=NORMAL`, 30s
  `busy_timeout`, 64 MB cache, `temp_store=MEMORY`, 256 MB mmap, `foreign_keys=ON`,
  `wal_autocheckpoint=1000`; periodic `PRAGMA wal_checkpoint(PASSIVE)` in the
  scheduler loop (app.py:1741-1747). Leave as-is.
- **Hot-column indexes (models.py + app.py:1770-1777):** all columns the
  aggregates and filters touch are indexed (§1.3). Leave as-is.
- **GROUP BY aggregates instead of ORM N+1** in `/api/artists` (app.py:224-240)
  and the two-query eager-load in `/api/artist/<id>` (app.py:550-565). The
  aggregate *queries* are the right shape — they're just recomputed per request,
  which §2 eliminates.
- **Album-level `is_downloaded`/`size_bytes` maintenance** (queue_service.py:
  847-852 and 1311-1316, watcher_service.py:163-167, metadata_service.py:
  215/280/346) — the album analogue of the artist counters already exists and is
  correct; keep the pattern, mirror it for artists.
- **Queue worker single-job 1.5s poll** (queue_service.py:1007-1029) — flag
  only, don't build batched claiming (brief §5.5).
- **SQLite stays** — documented non-goal (wayfinder/map.md standing preferences;
  plugin-architecture-map.md:87; brief §5.6). No Postgres.
- `/api/queue` history already capped at 50 (app.py:1125).

---

## 7. Summary — recommended changes in priority order

1. **Denormalize per-artist counters** (`Artist.total_albums/total_tracks/downloaded_tracks`,
   §2.1): boot-time `ALTER TABLE` + one-shot backfill (§2.2), then a single
   `_bump_artist` helper called at the exhaustive write-point list (§2.3 — the 42
   cited mutations across queue_service, app.py, import_service, metadata_service,
   watcher_service, lidarr_service, scripts/reverify_library.py), with the
   `track.after_download`/`track.verified` event hooks (INTEGRATION.md §5) also
   wired to the helper for Phase-2 plugin-chain paths. `/api/artists` then reads
   columns instead of the two GROUP BY scans; **response keys unchanged** (§2.5).
2. **Paginate `/api/artists` + cap `/api/queue` active** (§3): offset+limit now,
   keyset cursor `(name, id)` when artists exceed tens of thousands; switch the
   frontend (`app.js:291-363`, `index.html:133/142`) to paged fetch + load-more
   above ~2,000 artists, moving the client filter server-side; bound the
   unbounded active list at app.py:1124. Defer `/api/artist/<id>` per-album lazy
   loading as a flagged follow-up.
3. **FTS5: deferred/optional** (§4) — no LIKE scans exist in the search paths;
   only the bounded dedup `ilike` at queue_service.py:630.
4. **Plugin write-lock discipline** (§5): keep `call_safe` commit-free; buffer
   health-log rows and flush them piggybacked on queue_service's existing
   commits (1023/854/900/924/952); never commit from event hooks.
5. **Don't touch** (§6): WAL/tuning, hot-column indexes, the GROUP BY shapes
   themselves, album-level stats maintenance, 1.5s queue poll, SQLite-as-non-goal.
