# MusicBrainz catalogue integration — research findings & design

Status: research (no code changed). Companion ticket: `wayfinder/tickets/musicbrainz-integration.md`.
Scope: enrichment-only catalogue source for fnack's discography build. Deezer stays authoritative;
MusicBrainz (MB) never removes, renames, or reorders existing data — it only *adds* release-group ids,
canonical names, and (optionally) cover-art fallbacks on confident matches.

---

## 1. API facts

### 1.1 Base URL, format, auth

- Base URL: `https://musicbrainz.org/ws/2/`
- JSON via `?fmt=json` query param (or `Accept: application/json` header).
- **Zero authentication** for reads — the OAuth machinery is only for editing MB itself.
- Pagination on search: `limit` (default 25, max **100**) and `offset`. Every search result returns
  `count` (total hits) and each hit carries a `score` (0–100, relative within that query — see
  §2.1; do not treat raw score as an absolute confidence measure).

Sources: [MusicBrainz API / Search](https://musicbrainz.org/doc/MusicBrainz_API/Search),
[MusicBrainz API / FAQ](https://musicbrainz.org/doc/MusicBrainz_API/FAQ).

### 1.2 User-Agent — hard requirement

- MB **requires** a descriptive User-Agent identifying the app, its version, and a contact
  (URL or email). Requests without a proper UA get **HTTP 403**.
- Recommended: `fnack/0.2.x ( +https://github.com/<repo>; contact: <admin email> )`.
- Set it once on the shared `requests.Session` (same pattern as `deezer_service._session`).
  MB docs explicitly ask for a real contact so they can reach you if your client misbehaves.

### 1.3 Rate limiting — 1 req/s etiquette, 503 + Retry-After

- Etiquette: **max ~1 request/second average** per client IP.
- Exceeding it returns **HTTP 503** with a **`Retry-After`** header (seconds to wait).
  Honor it, sleep, retry. Do not hammer; MB rate-limits per-IP.
- Nuance (from the musicbrainzngs tracker): MB's limiter actually tolerates bursts up to what the
  server can process, but the documented, safe contract is 1 req/s. fnack syncs are background jobs,
  so be conservative: a global 1.0 s pacing between MB calls.
- Note for fnack: requests egress through `vpn_service` when enabled, so the limiting IP is the
  VPN's, not the container's. The local 1 req/s throttle is what protects us regardless.
- 403 can also mean "rate limited via bad UA" — check UA before blaming the limit.

Sources: [MusicBrainz API / Rate Limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting),
[New Web Service policy for NGS (blog)](https://blog.musicbrainz.org/?p=839),
[musicbrainzngs rate-limit nuance (issue #204)](https://github.com/alastair/python-musicbrainzngs/issues/204),
[musicbrainzngs docs](https://python-musicbrainzngs.readthedocs.io/en/v0.4/api/).

### 1.4 Endpoints that matter for discography cleaning

All are `GET /ws/2/<entity>?...&fmt=json`.

| Entity | Search (Lucene `query=`) | Lookup |
|---|---|---|
| artist | `/artist/?query=artist:"Name"` | `/artist/{mbid}?inc=release-groups+aliases` |
| release-group | `/release-group/?query=releasegroup:"Title" AND artist:"Name"` | `/release-group/{rgid}?inc=releases` |
| release | `/release/?query=release:"Title" AND artist:"Name"` | `/release/{reid}?inc=recordings+artist-credits+isrcs` |
| recording | `/recording/?query=recording:"Track" AND artist:"Name"` | `/recording/{mbid}?inc=isrcs` |
| ISRC | `/recording/?query=isrc:GBUM71704541` | — |

Useful search fields (Lucene syntax, `field:"exact phrase"`):
`artist`, `arid`, `releasegroup`, `rgid`, `release`, `reid`, `recording`, `isrc`,
`primarytype` (Album|Single|EP|Other|Broadcast), `secondarytype` (Compilation|Soundtrack|Live|
Remix|DJ-mix|Mixtape/Street|Demo|Audiobook|Interview|Spokenword), `firstreleasedate`, `releasedate`,
`date`, `country`, `label`, `barcode`, `type`, `status`, `comment`.

Queries compose with `AND`/`OR` and quotes, e.g.
`releasegroup:"Jatt Te Jawani" AND arid:1234-...`. Keep queries simple — exact-phrase on a
normalized title beats clever boolean queries.

Key design consequence: **an artist lookup with `inc=release-groups` returns the artist's whole
logical discography in one request** (each release-group has `id`, `primary-type`,
`secondary-types`, `first-release-date`). That is 1 request per artist instead of 1 per album, and
it sidesteps per-album search noise. Use release-group *search* only as a fallback when the artist
lookup misses something.

Sources: [MusicBrainz API / Search](https://musicbrainz.org/doc/MusicBrainz_API/Search),
[ReleaseGroupSearch](https://musicbrainz.org/doc/MusicBrainz_API/Search/ReleaseGroupSearch),
[RecordingSearch](https://musicbrainz.org/doc/MusicBrainz_API/Search/RecordingSearch).

### 1.5 release-group vs release — the "clean up a discography" model

- **Release-group** = the abstract logical album: canonical title, artist credit, `primary-type`
  (Album / Single / EP / Other / Broadcast), `secondary-types` (Compilation, Soundtrack, Live,
  Remix, DJ-mix, Mixtape/Street, Demo…), `first-release-date`, and its own MBID (`rgid`).
  No tracklist of its own.
- **Release** = one concrete edition of a group: format (CD/LP/digital), country, label, catalog
  number, barcode, edition date, `status` (official/promotion/bootleg), and the tracklist of
  `recording`s for that edition. Has its own MBID (`reid`).
- One release-group contains many releases: deluxe edition, remaster, reissue, country variants,
  different formats. **For discography purposes the release-group is the dedup key** — it is
  exactly the "one row per logical album" model fnack wants next to Deezer's album row.
- `first-release-date` of the group = the earliest release date (original issue, *not* the
  reissue year). This is the field to compare against Deezer's album year, with the caveat in §2.2
  that a Deezer "reissue" row may legitimately differ by years.
- Enrichment therefore stores the **rgid**, the **canonical (group) title**, and the
  **first-release year** — never release-level detail (editions) — unless we later want per-edition
  track verification via `/release/{reid}?inc=recordings+isrcs`.

Sources: [Release_Group concept](https://musicbrainz.org/doc/Release_Group),
[Release concept](https://musicbrainz.org/doc/Release),
[Album types discussion (Plex forum)](https://forums.plex.tv/t/how-to-album-types-and-editing-musicbrainz/736980).

### 1.6 Cover Art Archive (CAA)

- Separate host, no auth, **not** subject to the 1 req/s MB Web-Service limiter (CDN-backed), but
  still be polite (small throttle + cache; CAA docs recommend caching and using thumbnails).
- `https://coverartarchive.org/release-group/{rgid}/front-500` — direct 500px front art
  (also `front-250`, `front-1200`, `front`); 302-redirects to the image.
- `https://coverartarchive.org/release-group/{rgid}` — JSON of images aggregated across the
  group's releases; each image has `thumbnails` {250, 500, 1200, small, large} and a `release`
  field. This is the right call for fnack because we hold an rgid, not a reid.
- `https://coverartarchive.org/release/{reid}/...` variants exist for edition-level art.
- **404** means no cover art (old/obscure releases) — cache that as a negative so we don't
  re-request every sync.
- fnack already has reliable Deezer covers, so CAA is a **fallback only** (or disabled by config).

Sources: [Cover Art Archive / API](https://musicbrainz.org/doc/Cover_Art_Archive/API),
[Announcing the Cover Art Archive (blog)](https://blog.metabrainz.org/2012/10/09/announcing-the-cover-art-archive/).

---

## 2. Matching policy

Goal: attach MB enrichment to Deezer albums **only when the identity is confident**; degrade to
Deezer-only for regional artists and stale MB data. Normalization reuses the existing
`_normalize()` from `services/itunes_service.py` (strips `(feat…)`/`(Deluxe…)`/`- Single`/`- EP`,
NFKD, keep alnum, lowercase) so MB matching speaks the same dialect as the iTunes complement.

### 2.1 Artist resolution (one-time per artist per cache window)

1. `GET /ws/2/artist/?query=artist:"<name>"&limit=10&fmt=json`.
2. Require **normalized exact-equality** of the hit's `name` against the Deezer artist name —
   *not* the raw `score` (scores are query-relative). Tie-break by score among exact-name hits.
3. If no hit has an exact normalized name: do one `GET /ws/2/artist/{top_mbid}?inc=aliases` and
   accept if any alias (normalized, script-insensitive — Punjabi artists often have romanized
   variants like "Gur Sidhu" / "Gursidhu") equals the Deezer name. Otherwise → **not in
   MusicBrainz**.
4. Outcome recorded per artist (positive: mbid + release-groups payload; negative: "no artist").
   Negative outcome is the *regional-artist path*: nothing else happens for this artist — no
   per-album probing — Deezer's discography is kept as-is.

### 2.2 Album ↔ release-group matching (per Deezer album, against the artist's release-groups)

Scoring (start values; tune after a pilot):

| Signal | Weight |
|---|---|
| Normalized title equality (**hard gate** — no match without it) | 40 |
| Year agreement: MB `first-release-date` year within **±1** of Deezer year, or either missing | 30 |
| Type compatibility: MB primary-type ↔ Deezer `record_type` (Album↔album; Single/EP↔single/ep; allow Album↔single/ep since Deezer often calls EPs "album") | 30 |
| **Threshold for enrichment** | **≥ 70** |
| Ambiguous (40–69): log "ambiguous", **do not enrich** | — |

- Title equality uses the group title vs Deezer album title after `_normalize` (which already
  strips `(feat…)`, `(Deluxe…)`, `- Single`/`- EP`, etc. — covers the common edition noise).
- Year mismatch > ±1 does **not** disqualify, it just drops confidence (covers MB
  `first-release-date` = original issue vs Deezer reissue rows). This is why remasters/reissues
  still enrich when title+type agree (70) but a coincidental same-title different-year album does
  not (40).
- Ties (two groups with identical normalized title + year): pick the higher score, then the
  lexicographically earliest group, log it, move on. Never fail the sync over ambiguity.
- When matching an ambiguous album, optionally consult track counts via
  `release-group/{rgid}?inc=releases` + one release's `inc=recordings` — but only when a config
  flag `musicbrainz.deep_verify` is on; each such probe costs requests. Default: skip (cheap path).

### 2.3 What we write on a confident match (enrichment-only)

- `Album.mb_release_group_id` (rgid) — the dedup key.
- `Album.mb_canonical_title`, `Album.mb_first_release_year` — **additional** metadata columns,
  never overwriting Deezer `name`/`year` (display stays Deezer's).
- CAA cover art (if `musicbrainz.cover_art` enabled and Deezer `cover_url` missing) keyed by rgid.
- Nothing is deleted, renamed, filtered, or reordered based on MB. MB is a metadata *suggester*,
  not an authority in fnack.

### 2.4 Regional artists / "not in MB" (Happy Raikoti, Arjan Dhillon, Gur Sidhu…)

- Artist resolution fails → record negative cache entry (30-day TTL) → zero MB requests for that
  artist's albums. Deezer discography unchanged. This is the default, most common path.
- Album has no release-group match (MB artist exists but discography is incomplete/stale) → no
  per-album enrichment, no error. Deezer row stays as the single source of truth.
- **Stale MB data rule:** MB can only *add*. If MB lacks a recent album, we simply don't enrich it.
  If MB's canonical title differs from Deezer's, both coexist (Deezer name displayed, MB canonical
  stored as metadata). If MB calls it a Single and Deezer an Album, `record_type` is untouched.
- Never use MB to remove a Deezer album, change a year, or rename a track/album in place.

### 2.5 Verification by ISRC (cheap, optional, default off)

- Yes — include it, gated behind `musicbrainz.verify_isrc` (default off), because it is cheap
  **only with caching**: ISRCs are immutable identifiers, so a per-ISRC result cache is permanent
  (one-time cost per ISRC, never re-queried).
- Where it runs: only for albums that matched a release-group (enrichment exists to anchor
  context), and capped at **K = 3 tracks per album**.
- Lookup: `GET /ws/2/recording/?query=isrc:<12-char>&fmt=json&limit=1`.
  - Found + normalized recording title ≈ normalized Deezer track title → mark `verified`.
  - Found + title differs → **warning flag** on the track (never auto-rename).
  - Not found → ignore (MB ISRC coverage is incomplete, especially for regional music — absence
    proves nothing).
- Value: catches mismatched tracklists / wrong-file cases cheaply; feeds the existing
  "diagnose failing tracks" work with a soft signal. Do not block downloads on it.

---

## 3. Hook points

1. **Artist discography sync (primary hook).** `get_artist_discography()` in
   `services/deezer_service.py` returns `{artist_id, artist_name, albums:[…]}`; both call sites —
   `app.py:369` (manual/import flow) and `import_service.py:263` — persist it. Add a new
   `services/musicbrainz_service.py` exposing `enrich_artist_discography(artist, albums)` that runs
   *after* the Deezer(+iTunes) pass and *before* persistence, so the Album rows are written once
   with enrichment columns populated. Keep it fail-soft: any MB exception is caught inside the
   service and logged; the Deezer discography persists regardless. Wrapping both call sites is fine
   (the enrichment is idempotent thanks to §4 caching).
2. **Download verification (optional hook).** In `verifier_service.py`, if
   `musicbrainz.verify_isrc` is on and the track's album has an rgid, run the §2.5 ISRC check.
   Async-friendly: the ISRC cache lookup is a SQLite read; only cache misses hit the API (paced).
3. **Cover art fallback (optional).** When an Album row has `mb_release_group_id` and no
   `cover_url` (or on demand in metadata flows), fetch CAA `front-500` and store locally — cache
   keyed by rgid including 404s.

Schema deltas (SQLite WAL, SQLAlchemy):
- `albums`: add nullable `mb_release_group_id` (indexed), `mb_canonical_title`,
  `mb_first_release_year`, `mb_confidence`, `mb_checked_at`. **Note:** `db.create_all()`
  (app.py:1584) will not add columns to the existing table — needs a tiny startup `ALTER TABLE`
  migration (or drop/recreate dev DB). New tables below are created fine by `create_all`.
- New `musicbrainz_cache`: `key` = normalized artist name (PK), `status` (`found`/`notfound`/
  `error`), `mb_artist_id`, `payload` (JSON: release-groups), `checked_at`.
- New `mb_isrc_cache`: `isrc` (PK), `status` (`verified`/`mismatch`/`notfound`), `recording_mbid`,
  `mb_title`, `mb_artist`, `checked_at`.

---

## 4. Caching (rate-limit budget)

Budget model: 1 req/s, artists are the unit of sync. A 100-artist full sync would cost ~100–150
requests (~2–3 min wall time, spread across the sync's existing per-album sleeps). Caching must
make that mostly zero on re-syncs.

- **Artist cache (new `musicbrainz_cache` table).** Key: normalized artist name. TTL: **7 days**
  for `found`, **30 days** for `notfound` (regional artists: don't re-probe a known-empty artist
  every sync), **1 hour** after `error` (don't retry a dead MB within the same window). Stores the
  whole release-group payload, so one cache hit serves the artist's entire enrichment.
- **Album-level columns** (`mb_checked_at`, `mb_confidence`, rgid) double as a per-album cache:
  an album with `mb_checked_at` fresher than the artist cache TTL is never re-queried, even if the
  artist cache was evicted.
- **In-memory TTL dict** mirroring `deezer_service._search_cache` for the same-process repeated
  syncs (matches existing house style; cheap to add).
- **ISRC cache:** permanent (ISRCs are immutable) — keeps §2.5 truly one-time per ISRC.
- **CAA image cache:** local `covers/mb/{rgid}.jpg` + a negative marker file/table row for 404s;
  CAA's own docs recommend caching and thumbnail sizes.
- **Pacing:** a module-level monotonic-clock throttle enforcing **≥ 1.0 s between MB requests**
  (shared across services so concurrent syncs can't burst), plus honoring `Retry-After` on 503.
- **Retry:** `urllib3.Retry`-style or manual: 503/429 → honor `Retry-After` (default `1s * attempt`),
  max 5 attempts, total cap ~60 s per artist, then give up (mark cache `error`).

---

## 5. Failure handling

- **Fail-soft, always.** MB enrichment runs in a try/except that never raises into the discography
  build; Deezer path is untouched by MB outages. Log `[MUSICBRAINZ]` warnings like the Deezer
  service does.
- **403 (bad/missing UA)** → fix UA config, do not retry loop.
- **503 / rate limit** → read `Retry-After`, sleep, retry; on persistent 503, back off for the
  current sync and record `error` in the cache (1 h TTL) so the next sync doesn't immediately
  hammer again.
- **404 / 400 (bad query)** → no retry; treat as no-match/negative cache.
- **Network/timeout** → catch, log, leave cache untouched (retry next sync naturally).
- **Ambiguous matches** → log, don't enrich, don't fail.
- **Config knobs** (via existing `AppSetting` table or config file): `musicbrainz.enabled`
  (default on, but trivially flippable), `musicbrainz.ttl`, `musicbrainz.cover_art`,
  `musicbrainz.verify_isrc`, `musicbrainz.deep_verify`.

---

## 6. Open questions

1. **Column migration mechanics** — confirm whether fnack tolerates a startup `ALTER TABLE`
   for new `albums` columns, or whether a fresh `create_all` on existing DBs is acceptable in
   practice (Docker volume upgrade path).
2. **Pilot tuning** — validate §2.2 thresholds against real data: run enrichment on 3–5 major
   artists (e.g. a big international artist where MB is strong) and check precision of rgid
   assignments; tune weights if title-only matches misfire.
3. **Alias coverage for Punjabi artists** — how often do MB aliases actually rescue a match for
   Happy Raikoti / Arjan Dhillon / Gur Sidhu? If ~never, drop the aliases lookup (saves 1 request
   per artist) and rely purely on exact-name resolution. Measure before/after.
4. **CAA value for this library** — Deezer covers are usually present; is CAA fallback worth the
   storage? Decide from a small sample of matched rgids.
5. **ISRC verification scope** — does the Deezer ISRC data quality justify the (cached, capped)
   MB ISRC checks, or is it noise for regional tracks? Pilot on a few albums.
6. **Should MB enrichment also run for iTunes-complement albums?** (They have no Deezer id but do
   have an iTunes id; matching by normalized title/year works the same.)
7. **VPN egress** — confirm the 1 req/s throttle is per-process (it is), and that enabling
   `vpn_service` doesn't put all MB traffic on one shared IP in a way that matters (local throttle
   already bounds it).

---

## References

- [MusicBrainz API / Search](https://musicbrainz.org/doc/MusicBrainz_API/Search)
- [MusicBrainz API / FAQ](https://musicbrainz.org/doc/MusicBrainz_API/FAQ)
- [MusicBrainz API / Rate Limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)
- [New Web Service policy for NGS (blog)](https://blog.musicbrainz.org/?p=839)
- [ReleaseGroupSearch](https://musicbrainz.org/doc/MusicBrainz_API/Search/ReleaseGroupSearch)
- [RecordingSearch](https://musicbrainz.org/doc/MusicBrainz_API/Search/RecordingSearch)
- [Cover Art Archive / API](https://musicbrainz.org/doc/Cover_Art_Archive/API)
- [Release_Group concept](https://musicbrainz.org/doc/Release_Group) / [Release concept](https://musicbrainz.org/doc/Release)
- [musicbrainzngs (python client reference for conventions)](https://python-musicbrainzngs.readthedocs.io/en/v0.4/api/)
- [musicbrainzngs rate-limit nuance (issue #204)](https://github.com/alastair/python-musicbrainzngs/issues/204)
