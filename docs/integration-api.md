# Integration API (v1) — accounts, library, and per-account user state

fnack core exposes a small, **protocol-agnostic** HTTP API that external
systems can build on: a server product that speaks its own client protocol, a
sync tool, a desktop client, or a script. Core implements no media-server
protocol itself — it provides the reusable primitives (accounts, library
queries, per-account user state) and the external system decides what to
expose to its own clients.

Base path: `/api/integration/v1` — see `GET /api/integration/v1/meta` for the
machine-readable capability list.

```
external server (its own client protocol)  ─┐
external tools / scripts                   ─┼─►  fnack core integration API
any HTTP client                            ─┘     accounts + library + user state
```

## Authentication

Every endpoint needs an **account identity** except `POST /auth/token` and
`GET /meta`. Two credential types are accepted:

| Credential | How it is sent | Notes |
|---|---|---|
| Account session | `Cookie: session=…` | What the fnack web UI uses (form login at `/login`). |
| Account API token | `Authorization: Bearer fnack_…` or `X-API-Token: fnack_…` | For programmatic clients. **Recommended for integrations.** |

A token authenticates **as its account**: the API derives the account from the
credential and never accepts an account id from the caller. Tokens are
generated with 256 bits of randomness, prefixed with `fnack_`, and stored
**only as a SHA-256 hash** — the plaintext is returned once at creation and
can never be recovered from the database.

The machine-level API key used elsewhere in fnack does **not** authenticate
here: it carries no account and therefore cannot honour per-account isolation.

### Get a token

```bash
curl -sX POST http://localhost:4688/api/integration/v1/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice", "password": "…", "label": "living-room"}'
# → 201 {"token": "fnack_…", "token_type": "bearer", "account": {...}, …}
```

```bash
TOKEN=fnack_…
curl -s http://localhost:4688/api/integration/v1/me -H "Authorization: Bearer $TOKEN"
```

### Manage tokens

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/tokens` | List this account's tokens (never the secret). |
| `POST` | `/tokens` | Create one (`label`, `expires_in_days`). |
| `DELETE` | `/tokens/<id>` | Revoke one (another account's id → `404`). |
| `GET` | `/me` | Identity + this account's state counts + library totals. |

## Library queries

Provider-neutral: no metadata provider is required, and provider identities
(`provider_id`, `external_id`) are returned for display only — they are
opaque, separately scoped values, never a primary reference.

| Method | Path | Query parameters |
|---|---|---|
| `GET` | `/library/stats` | — (artist/album/track/downloaded counts) |
| `GET` | `/library/artists` | `q`, `offset`, `limit`, `sort` (`name`,`created`,`id`), `order` |
| `GET` | `/library/artists/<id>` | — (with album/track counts) |
| `GET` | `/library/albums` | `q`, `artist_id`, `offset`, `limit`, `sort` (`name`,`year`,`created`,`id`), `order` |
| `GET` | `/library/albums/<id>` | — (with track/downloaded counts) |
| `GET` | `/library/tracks` | `q`, `album_id`, `artist_id`, `downloaded`, `status`, `offset`, `limit`, `sort` (`title`,`duration`,`track_number`,`created`,`id`), `order` |
| `GET` | `/library/tracks/<id>` | — (includes `stream_path` / `stream_ready` for serving audio) |

`q` on tracks matches the track title, its album name **or** its artist name,
so a caller does not need to know which level to search. Every list response
is `{"items": [...], "total": n, "offset": …, "limit": …, "has_more": bool}`
and is paginated in SQL (`LIMIT`/`OFFSET` + `COUNT`) — integrations never load
the library into memory. `limit` is capped at 500 per request.

## Per-account user state

Every object below is owned by exactly one account. Reads and writes are
scoped to the authenticated account; an id belonging to another account
returns `404` (existence is never leaked). All references are fnack's
**internal** `Artist`/`Album`/`Track` ids, validated before anything is
persisted (unknown ids → `400`).

### Playlists

| Method | Path | Body / notes |
|---|---|---|
| `GET` | `/playlists` | `offset`, `limit`; each entry has `item_count`. |
| `POST` | `/playlists` | `{"name", "comment"?, "track_ids"?}` → `201`. |
| `GET` | `/playlists/<id>` | Full ordered items (`offset`/`limit` optional). |
| `PATCH` | `/playlists/<id>` | `{"name"?, "comment"?}`. |
| `DELETE` | `/playlists/<id>` | Removes the playlist and its items. |
| `POST` | `/playlists/<id>/items` | `{"track_ids": [...], "position"?}` → append or insert. |
| `PUT` | `/playlists/<id>/items` | `{"item_ids": [...]}` — the exact full order. |
| `DELETE` | `/playlists/<id>/items/<item_id>` | Removes one entry. |

**Ordering is deterministic.** Each item row carries a `position` that is
unique per playlist and always compacted to `0..n-1`; reads are ordered by
`(position, id)`. Reordering is a two-phase move inside one transaction, so
two rows can never transiently share a position; and `PUT …/items` is refused
(`400`) unless it lists exactly the playlist's item ids — a partial or
duplicated order is never guessed.

Each item also carries a **snapshot** (`title`, `artist_name`, `album_name`,
`duration`, `isrc`) plus `available`: if the referenced track is not in the
library any more, the entry still renders from its snapshot with
`available: false` instead of disappearing.

### Favorites / stars

| Method | Path | Notes |
|---|---|---|
| `GET` | `/favorites` | `type` (`artist`\|`album`\|`track`), `offset`, `limit`. |
| `GET` | `/favorites/<type>/<id>` | `{"starred": bool}` |
| `PUT` | `/favorites/<type>/<id>` | Star (idempotent). |
| `DELETE` | `/favorites/<type>/<id>` | Unstar. |

### Ratings

| Method | Path | Notes |
|---|---|---|
| `GET` | `/ratings` | `type`, `offset`, `limit`. |
| `GET` | `/ratings/<type>/<id>` | `{"rating": 1..5 \| null}` |
| `PUT` | `/ratings/<type>/<id>` | `{"rating": 1..5}` (other values → `400`). |
| `DELETE` | `/ratings/<type>/<id>` | Clears the rating. |

### Bookmarks

| Method | Path | Notes |
|---|---|---|
| `GET` | `/bookmarks` | `offset`, `limit`. |
| `GET` | `/bookmarks/<type>/<id>` | `404` when unset. |
| `PUT` | `/bookmarks/<type>/<id>` | `{"position_ms": int, "comment"?}`; `type` is `track`\|`album`. |
| `DELETE` | `/bookmarks/<type>/<id>` | Removes it. |

### Scrobble / play history

| Method | Path | Notes |
|---|---|---|
| `POST` | `/scrobbles` | `{"track_id", "played_at"?, "submission"?}` → `201`. |
| `GET` | `/scrobbles` | `offset`, `limit`, `since` (ISO-8601), newest first. |
| `GET` | `/scrobbles/top` | Most-played tracks (`limit`). |

### Saved play queue

| Method | Path | Notes |
|---|---|---|
| `GET` | `/queue` | Entries + `current_index`, `position_ms`, `changed_by`, `changed_at`. |
| `PUT` | `/queue` | `{"track_ids": [...], "current_index"?, "position_ms"?, "changed_by"?}` — replaces atomically. |
| `DELETE` | `/queue` | Clears the queue. |

### Summary

`GET /state/summary` → `{"playlists", "favorites", "ratings", "bookmarks",
"scrobbles", "queue_items"}` for the authenticated account.

## State outlives the library

User state is deliberately **not** foreign-keyed to library rows:

* Metadata or provider changes to a track (title, duration, `provider_id`,
  `external_id`) are invisible to user state — it references the internal id.
* If a track row is **hard-deleted** (a discography sync prunes a release that
  the serving provider no longer lists, or an artist is deleted), fnack
  snapshots the affected state first. Playlist entries, queue entries,
  favorites, ratings, bookmarks and history stay readable and are marked
  `available: false`.
* If the same recording is re-created later (typically by a different
  provider, under a new internal id), state is **re-attached automatically by
  ISRC** — the users' stars, ratings, bookmarks, playlist and queue entries
  follow the recording rather than being lost.

## Security model

* Ownership is enforced in the service layer for every operation; the HTTP
  layer never accepts an account id from the caller.
* Cross-account access returns `404`, so one account cannot probe another's
  data by id.
* All referenced library ids are validated before persistence.
* Tokens are stored hashed, are revocable individually, and can carry an
  expiry; `last_used_at` is recorded for auditing.
* The web UI's session guard and CSRF rules also apply to this API when a
  session cookie is used; token requests carry no cookie and are therefore not
  subject to cross-site request forgery.

## Migration

The user-state and token tables (`playlists`, `playlist_items`, `favorites`,
`ratings`, `bookmarks`, `scrobbles`, `play_queues`, `play_queue_entries`,
`api_tokens`) are created **additively and idempotently** at startup
(`services/schema_migrations.py`), so an existing installation keeps every
library row and every account and gains the new tables on the next boot.

## Example session

```bash
BASE=http://localhost:4688/api/integration/v1
TOKEN=$(curl -sX POST $BASE/auth/token -H 'Content-Type: application/json' \
          -d '{"username":"alice","password":"…"}' | jq -r .token)
AUTH="Authorization: Bearer $TOKEN"

# search the library, page it
curl -s "$BASE/library/tracks?q=live&limit=25&sort=title" -H "$AUTH"

# create a playlist from the first result
TRACK=$(curl -s "$BASE/library/tracks?limit=1" -H "$AUTH" | jq -r '.items[0].id')
PL=$(curl -sX POST $BASE/playlists -H "$AUTH" -H 'Content-Type: application/json' \
       -d "{\"name\":\"Road trip\",\"track_ids\":[$TRACK]}" | jq -r .id)

# star it, rate it, remember where you are, save the queue
curl -sX PUT "$BASE/favorites/track/$TRACK" -H "$AUTH"
curl -sX PUT "$BASE/ratings/track/$TRACK" -H "$AUTH" -H 'Content-Type: application/json' -d '{"rating":5}'
curl -sX PUT "$BASE/bookmarks/track/$TRACK" -H "$AUTH" -H 'Content-Type: application/json' -d '{"position_ms":42000}'
curl -sX PUT "$BASE/queue" -H "$AUTH" -H 'Content-Type: application/json' \
     -d "{\"track_ids\":[$TRACK],\"current_index\":0,\"changed_by\":\"road-trip\"}"

# record a play
curl -sX POST "$BASE/scrobbles" -H "$AUTH" -H 'Content-Type: application/json' \
     -d "{\"track_id\":$TRACK}"
```
