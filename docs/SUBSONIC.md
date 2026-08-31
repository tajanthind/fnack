# Subsonic / OpenSubsonic API (`fnack.subsonic` plugin)

fnack ships a `server_extension` plugin (`fnack.subsonic`) that turns the app
itself into a Subsonic-API-compatible media server: any Subsonic client
(Symfonium, DSub, Sublime Music, Feishin, Substreamer, Tempus, play:Sub, ...)
can browse, search and stream fnack's library directly, without Navidrome or
any other server in between.

The plugin is bundled, official, and **enabled by default** — everything under
`/rest/*` is live as soon as fnack runs. Disabling it from Settings → Plugins
removes the routes (routes are registered only for enabled server-extension
plugins).

## Endpoint coverage

Version 2.0.0 implements (each in plain and `.view` spelling):

| Endpoint | Notes |
|---|---|
| `ping` | Envelope check. |
| `getLicense` | Perpetual license (`valid=true`, fnack is free software). |
| `getMusicFolders` | One folder: `id=0`, "fnack". |
| `getIndexes` / `getArtists` | A–Z index; leading articles ignored ("The Beatles" → B); accents folded (Édith → E). |
| `getArtist` | Albums sorted by year with songCount/duration. |
| `getMusicDirectory` | `0` → root (artists), `ar-…` → albums, `al-…` → songs (v1-style tree for old clients). |
| `getAlbum` / `getSong` | Full song attributes (duration, bitrate, size, contentType, suffix, genre, track/disc, path). |
| `getAlbumList` / `getAlbumList2` | `alphabeticalByName`, `alphabeticalByArtist`, `newest`, `byYear` (+fromYear/toYear; a reversed range sorts newest-first per spec), `byGenre`, `random`; `highest`/`frequent`/`recent`/`starred` degrade to alphabetical (fnack tracks no play counts). |
| `search2` / `search3` | Artist/album/song substring search (`search_library`); an empty query lists the library (Navidrome behaviour). |
| `getSimilarSongs` / `getSimilarSongs2` | AudioMuse-AI when enabled (see below), else same-artist fallback. |
| `getArtistInfo` / `getArtistInfo2` | AudioMuse-AI when enabled, else other-library-artists fallback; not-stocked similar artists use the `id="-1"` convention. |
| `getGenres` | From track genre tags. |
| `getRandomSongs` | With `size`/`genre`/`fromYear`/`toYear`. |
| `getCoverArt` | `al-…` serves the album folder's `cover.jpg`/`folder.jpg` (or your configured `cover_art_filename`), falling back to the stored remote cover URL; `ar-…` serves the artist image. The `size` param is accepted but not applied (no image library in the container). |
| `stream` / `download` | Raw file delivery, exact bytes. `maxBitRate`/`format`/`timeOffset` are accepted and ignored — the container has no ffmpeg, so no transcoding. |
| `getScanStatus` / `startScan` | Count = tracks in the DB; startScan emits fnack's `library.scan_requested` event. |
| `getStarred` / `getStarred2` / `star` / `unstar` | Empty/no-op (fnack has no favourites store) so clients don't error. |
| `getPlaylists` / `getVideos` | Empty lists. |
| `getOpenSubsonicExtensions` | Reachable without auth (OpenSubsonic requirement); no extensions advertised. |

Not implemented: transcoding, `getChatMessages`, `getPodcasts`, `getBookmarks`,
jukebox, sharing. Clients that require them will show those sections as empty.

### Wire format

XML by default (spec), JSON via `f=json`, JSONP via `f=jsonp&callback=…`.
Verified against the 1.16.1 XSD, the official Subsonic 6.1.6 demo server and
Navidrome's golden responses:

- Repeated child elements are **always JSON arrays** (even with one element).
- Empty repeated children **omit the key entirely**; the wrapper stays
  present-but-empty (`{"searchResult3":{}}`, `"similarSongs2": {}`).
- JSON never emits `null`; absent attributes are omitted.
- Element text content maps to `"value"` in JSON (`getGenres`).
- `subsonic-response` carries `status`, `version`, `type="fnack"`,
  `serverVersion` and `openSubsonic="true"` (JSON: `true`).
- `getOpenSubsonicExtensions` is implemented and reachable **without auth**
  (per OpenSubsonic); fnack advertises no extensions beyond the base
  envelope.
- Binary endpoints (`stream`, `download`, `getCoverArt`) report failures as
  an **XML error document** with `Content-Type: text/xml` regardless of
  `f` — clients sniff the content type to detect errors.

Errors use the standard codes: `10` missing parameter, `40` wrong
username/password, `70` data not found — always HTTP 200, per spec.
`getSimilarSongs(2)`/`getArtistInfo(2)` return `ok` with an empty wrapper
when they have nothing (error `70` is reserved for unknown ids), matching
Navidrome's behaviour without a similarity provider.

### IDs

Stable, prefixed (same convention as Navidrome): `ar-<artist row id>`,
`al-<album row id>`, `tr-<track row id>`, music folder `1`.

## Authentication

Subsonic clients send credentials as query params on every request. fnack
authenticates against its own M2M API key (`context.library.get_api_key()`):

- **No API key configured** → the API is open (fnack's zero-required-auth
  model). Any `u`/`p` is accepted, none required.
- **API key configured** → all of these work:
  - `p=<key>` (plaintext),
  - `p=enc:<hex-encoded key>`,
  - `t=md5(<key>+<salt>)&s=<salt>` (Subsonic token auth),
  - `apiKey=<key>` (OpenSubsonic API-key extension — this is what
    AudioMuse-AI sends when you point it at fnack in API-key mode).

The API key is the *password*; the `u` username is accepted as any value.

## Library visibility

fnack's DB is a *discography* model: it tracks albums/tracks that have not
been downloaded yet. The Subsonic API only surfaces **downloaded** tracks
(`is_downloaded`) and albums that contain at least one downloaded track, so
clients never see unplayable entries. Undownloaded rows are reachable only by
explicit id and return error `70` on `stream`.

## AudioMuse-AI integration (optional, off by default)

[AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) is a separate,
user-run container that analyses a library locally (Librosa/ONNX) and answers
sonic-similarity queries. fnack never runs it — it can only *talk* to one.
When the integration is off, nothing changes anywhere in fnack.

Settings (Settings → Plugins → Subsonic API):

| Key | Type | Default | Meaning |
|---|---|---|---|
| `audiomuse_enabled` | boolean | `false` | Master switch. |
| `audiomuse_url` | string | *(empty)* | Base URL, e.g. `http://audiomuse-ai-flask-app:8000` (container port 8000). |
| `audiomuse_api_token` | secret | *(empty)* | AudioMuse-AI **API token** (Setup Wizard → authentication; sent as `Authorization: Bearer …`). Leave empty for instances with `AUTH_ENABLED=false`. |
| `audiomuse_server` | string | *(empty)* | Optional multi-server selector passed as the `server` query param (only for AudioMuse-AI multi-media-server installs). |

### What it changes

When enabled, `getSimilarSongs(2)` and `getArtistInfo(2)` call AudioMuse-AI
first:

- `GET <url>/api/similar_tracks?title=<t>&artist=<a>&n=<count>&eliminate_duplicates=true`
  → bare JSON array of `{item_id, title, author, album, distance, …}`
- `GET <url>/api/similar_artists?artist=<name>&n=<count>`
  → bare JSON array of `{artist, artist_id, divergence, …}`

(Wire contract verified against AudioMuse-AI v3.5.1 source — `app_ivf.py`,
`app_artist_similarity.py`, `app_auth.py` — and its official Navidrome
plugin's Go client. The title+artist lookup form is used rather than
`item_id`, so it works no matter which media server AudioMuse-AI is
connected to.)

Results are matched back to fnack's library so every `similarSong` carries a
real, playable fnack id: direct `tr-…` id match when AudioMuse-AI is pointed
at fnack's own `/rest` API, artist+title string match otherwise. Entries that
don't correspond to anything in fnack's library are dropped (similar songs)
or returned id-less (similar artists, which the spec allows).

**Fallback policy (documented choice):** if AudioMuse-AI is unreachable,
misconfigured, or errors — including 401 — fnack logs a warning and serves
its local fallback instead of failing: other downloaded songs by the same
artist for `getSimilarSongs`, other library artists for `getArtistInfo`.
Clients therefore never see an error from these endpoints. When the
integration is disabled they serve the same fallback only.

### Two ways to wire it

1. **AudioMuse-AI → fnack (recommended).** fnack *is* a Subsonic server now,
   so point AudioMuse-AI itself at fnack: media server type `navidrome`
   (Subsonic-API), `NAVIDROME_URL=http://fnack:8000`, fnack's API key (or
   user+password), and let it analyse fnack's library. Then fnack's
   similarity endpoints answer from AudioMuse-AI's analysis of that very
   library, and id matching is exact.
2. **AudioMuse-AI → Navidrome (alongside fnack).** If AudioMuse-AI already
   analyses your Navidrome instance and fnack serves the same files, the
   artist+title matching still resolves similarity answers into fnack ids.

### Manual live test (docker compose)

With fnack running (`docker compose up -d`) and a library downloaded:

```bash
# (a) Subsonic client basics: browse, search, stream
F=http://localhost:8000   # your fnack host:port
curl -s "$F/rest/ping.view?f=json" | jq .
curl -s "$F/rest/getArtists?f=json" | jq '.["subsonic-response"].artists.index[0]'
curl -s "$F/rest/search3?query=<some+track+name>&f=json" | jq .
curl -s "$F/rest/getAlbumList2?type=alphabeticalByName&f=json" | jq .
curl -s -o /tmp/song.flac -w "%{http_code} %{content_type}\n" \
     "$F/rest/stream?id=tr-1&f=json" && file /tmp/song.flac
curl -s -o /tmp/cover.jpg -w "%{http_code} %{content_type}\n" \
     "$F/rest/getCoverArt?id=al-1&f=json"

# (b) Integration off → same behaviour as before this change; similarity
#     endpoints still answer with the same-artist fallback:
curl -s "$F/rest/getSimilarSongs2?id=tr-1&f=json" | jq .
curl -s "$F/rest/getArtistInfo2?id=ar-1&f=json" | jq .

# (c) Integration on → enable in Settings → Plugins → Subsonic API, set the
#     URL (+ API token), then:
curl -s "$F/rest/getSimilarSongs2?id=tr-1&count=10&f=json" | jq '.["subsonic-response"].similarSongs2.similarSong'
curl -s "$F/rest/getArtistInfo2?id=ar-1&count=10&f=json" | jq '.["subsonic-response"].artistInfo2.similarArtist'
#   → results now come from AudioMuse-AI (fnack logs a warning line when it
#     falls back, so silence + changed results = proxied answer).
docker logs fnack 2>&1 | grep -i audiomuse
```

### Automated test

`tests/run_subsonic_test.py` boots a real Flask app + the real plugin,
serves it over live HTTP, and checks ~50 assertions: all of (a), the
fallback half of (b), and (c) against a mock AudioMuse-AI speaking the exact
v3.5.1 wire contract (including Bearer auth, 401 fallback and unreachable
fallback):

```bash
.venv/bin/python tests/run_subsonic_test.py   # → SUBSONIC TEST PASSED
```
