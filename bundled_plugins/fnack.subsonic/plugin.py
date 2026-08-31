"""Bundled first-party plugin: Subsonic / OpenSubsonic API server extension.

Lets Subsonic clients (Symfonium, DSub, Sublime Music, Feishin, Tempus, ...)
browse, search and stream fnack's library directly — independent of
Navidrome — and optionally enriches similarity answers with a user-run
AudioMuse-AI instance.

Auth: Subsonic clients send u/p (or t=token&s=salt) as query params on every
request. We authenticate against fnack's M2M API key
(context.library.get_api_key()). If no API key is set, the API is open
(matches fnack's zero-auth model).

Wire format: XML by default (spec default); JSON via f=json, JSONP via
f=jsonp&callback=... . The response tree is built once in its JSON shape and
rendered to either format, following the Subsonic JSON conventions (repeated
elements are always JSON arrays — the Navidrome behaviour; element text
content maps to a "value" key).

Endpoints implemented (each also in its `.view` spelling):
  ping getLicense getMusicFolders getIndexes getArtists getArtist
  getMusicDirectory getAlbum getAlbumList getAlbumList2 getSong search2
  search3 getSimilarSongs getSimilarSongs2 getArtistInfo getArtistInfo2
  getGenres getRandomSongs getStarred getStarred2 getCoverArt stream
  download getScanStatus startScan getPlaylists getVideos
  getOpenSubsonicExtensions (no auth, per OpenSubsonic)

Wire notes (verified against the 1.16.1 XSD, official Subsonic 6.1.6 demo
responses, and Navidrome golden snapshots): XML default / f=json / f=jsonp;
repeated children are always JSON arrays; empty repeated children omit the
key (wrapper stays present-but-empty); nulls are never emitted; element text
content maps to a "value" key (getGenres); binary endpoints (stream/
download/getCoverArt) answer failures with an XML error doc regardless of f.

AudioMuse-AI integration (all settings off by default — strictly additive):
  audiomuse_enabled   boolean  master switch
  audiomuse_url       string   base URL, e.g. http://audiomuse-ai-flask-app:8000
  audiomuse_api_token secret   the instance's API token (Bearer auth), if set
  audiomuse_server    string   optional multi-server `server` selector
When enabled, getSimilarSongs(2)/getArtistInfo(2) ask AudioMuse-AI first and
fall back to fnack's local same-artist heuristic on any error (never error
out at clients). When disabled they serve the local heuristic only.
"""

import hashlib
import os
import random
import time
from datetime import timezone
from xml.sax.saxutils import quoteattr

from flask import Blueprint, Response, jsonify, request, send_file

from plugins.base import ServerExtensionPlugin

API_VERSION = "1.16.1"
# Subsonic's default ignoredArticles (used for index grouping).
IGNORED_ARTICLES = "The El La Los Las Le Les"

_MIME = {
    ".flac": "audio/flac", ".opus": "audio/ogg", ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".wma": "audio/x-ms-wma", ".aiff": "audio/aiff",
    ".aif": "audio/aiff", ".ape": "audio/x-ape", ".wv": "audio/x-wavpack",
}
_COVER_NAMES = ("cover.jpg", "folder.jpg", "cover.png", "folder.png")
_COVER_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp"}

# Stable per-process "last modified" for getIndexes (ms epoch).
_STARTED_AT_MS = int(time.time() * 1000)

def _fnack_version() -> str:
    try:
        from version import __version__
        return __version__
    except Exception:
        return "0.0.0"


def _epoch_iso() -> str:
    return "2000-01-01T00:00:00.000Z"


def _prune(node):
    """Subsonic JSON convention: empty lists are omitted entirely (the
    wrapper stays present-but-empty) and null values are never emitted.
    Official Subsonic 6.1.6 and Navidrome both verified to behave this way."""
    if isinstance(node, dict):
        out = {}
        for key, val in node.items():
            if val is None:
                continue
            if isinstance(val, list) and not val:
                continue
            out[key] = _prune(val)
        return out
    if isinstance(node, list):
        return [_prune(item) for item in node]
    return node


def _iso(dt) -> str:
    if not dt:
        return _epoch_iso()
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:
        return _epoch_iso()


def _index_letter(name: str) -> str:
    """Subsonic index group letter for an artist name ('#' for non-letters).

    Accents fold to their base letter (É → E) so clients see the familiar
    A–Z index; leading articles are ignored.
    """
    import unicodedata
    n = (name or "").strip()
    for art in IGNORED_ARTICLES.split():
        if n.lower().startswith(art.lower() + " "):
            n = n[len(art) + 1:]
            break
    ch = (n[:1] or "#").upper()
    folded = unicodedata.normalize("NFKD", ch)
    ch = "".join(c for c in folded if not unicodedata.combining(c)) or ch
    return ch if ch.isalpha() else "#"


class SubsonicPlugin(ServerExtensionPlugin):
    # ------------------------------------------------------------------
    # Settings (schema keys; all stored as strings by the settings store)
    # ------------------------------------------------------------------
    def _setting(self, key: str, default: str = "") -> str:
        val = self.context.settings.get(key)
        return default if val is None or val == "" else str(val)

    def _bool_setting(self, key: str) -> bool:
        return self._setting(key, "false").strip().lower() == "true"

    # ------------------------------- auth -----------------------------
    def _auth_ok(self, args) -> bool:
        key = self.context.library.get_api_key()
        if not key:
            return True  # zero-auth model: no key configured = open
        p = args.get("p")
        t = args.get("t")
        s = args.get("s")
        # OpenSubsonic API-key auth (used e.g. by AudioMuse-AI when pointed at
        # fnack): apiKey=<key> instead of u/p.
        api_key_param = args.get("apiKey")
        if api_key_param and api_key_param == key:
            return True
        if p:
            password = p
            if p.startswith("enc:"):  # p=enc:<hex-encoded password>
                try:
                    password = bytes.fromhex(p[4:]).decode("utf-8")
                except ValueError:
                    return False
            if password == key:
                return True
        if t and s and hashlib.md5((key + s).encode()).hexdigest() == t:
            return True
        return False

    # --------------------- response rendering ------------------------
    @staticmethod
    def _attr(value) -> str | None:
        """Stringify a JSON-shaped attribute value for XML output."""
        if value is None:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @classmethod
    def _to_xml(cls, name: str, value) -> str:
        """Render a JSON-shaped response subtree as Subsonic XML.

        dict  -> element with attributes (special key "value" = text content)
        list  -> repeated sibling elements
        scalar-> element text
        """
        if isinstance(value, list):
            return "".join(cls._to_xml(name, item) for item in value)
        if isinstance(value, dict):
            attrs = []
            text = None
            children = []
            for key, val in value.items():
                if key == "value" and not isinstance(val, (dict, list)):
                    text = cls._attr(val)
                elif isinstance(val, (dict, list)):
                    children.append(cls._to_xml(key, val))
                else:
                    attr = cls._attr(val)
                    if attr is not None:
                        attrs.append(f"{key}={quoteattr(attr)}")
            if text is not None:
                inner = (text.replace("&", "&amp;").replace("<", "&lt;")
                         .replace(">", "&gt;"))
            else:
                inner = "".join(children)
            if attrs:
                return f"<{name} {' '.join(attrs)}>{inner}</{name}>"
            return f"<{name}>{inner}</{name}>"
        text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<{name}>{text}</{name}>"

    def _envelope(self, payload: dict | None = None, error: dict | None = None) -> dict:
        body = {"status": "ok" if error is None else "failed",
                "version": API_VERSION,
                "type": "fnack",
                "serverVersion": _fnack_version(),
                "openSubsonic": True}
        if error is not None:
            body["error"] = error
        if payload:
            body.update(payload)
        return {"subsonic-response": body}

    def _respond(self, payload: dict | None = None, error: dict | None = None):
        body = self._envelope(payload, error)
        fmt = (request.values.get("f") or "xml").strip().lower()
        # Subsonic JSON convention: repeated children with no elements are
        # omitted entirely (wrapper stays present-but-empty), and nulls are
        # never emitted — verified against official Subsonic and Navidrome.
        body = _prune(body)
        if fmt == "json":
            return jsonify(body)
        if fmt in ("jsonp", "jsonp_func"):  # callback name in `callback` param
            callback = request.values.get("callback") or "callback"
            import json as _json
            return Response(f"{callback}({_json.dumps(body)});",
                            mimetype="text/javascript")
        xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               + self._to_xml("subsonic-response", body["subsonic-response"]))
        return Response(xml, mimetype="text/xml; charset=utf-8")

    def _xml_error(self, code: int, message: str):
        """Error doc for binary endpoints (stream/download/getCoverArt):
        always XML regardless of `f` — clients sniff content-type text/xml
        to detect failures (spec-verified behaviour)."""
        xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<subsonic-response xmlns="http://subsonic.org/restapi" '
               'status="failed" version="' + API_VERSION + '">' +
               self._to_xml("error", {"code": code, "message": message}) +
               "</subsonic-response>")
        return Response(xml, mimetype="text/xml; charset=utf-8")

    def _err(self, code: int, message: str):
        return self._respond(error={"code": code, "message": message})

    def _authorized(self):
        """Shared guard: returns an error Response or None."""
        if not self._auth_ok(request.values):
            return self._err(40, "Wrong username or password")
        return None

    # ------------------------ library views --------------------------
    def _artists(self) -> list[dict]:
        return self.context.library.list_artists()

    def _albums(self) -> list[dict]:
        return self.context.library.list_albums(limit=100000)

    def _tracks(self) -> list[dict]:
        return self.context.library.list_tracks(limit=100000)

    def _downloaded_tracks(self) -> list[dict]:
        return [t for t in self._tracks() if t.get("is_downloaded")]

    def _track_stream_path(self, t: dict) -> str | None:
        path = t.get("local_path") or t.get("file_path")
        return str(path) if path and os.path.isfile(str(path)) else None

    def _song(self, t: dict, albums_by_id: dict, artists_by_id: dict,
              parent: str | None = None) -> dict:
        album = albums_by_id.get(t.get("album_id")) or {}
        artist_id = t.get("artist_id") or album.get("artist_id")
        artist = artists_by_id.get(artist_id) or {}
        path = t.get("local_path") or t.get("file_path") or ""
        ext = os.path.splitext(str(path))[1].lower().lstrip(".")
        song = {
            "id": f"tr-{t['id']}",
            "parent": parent or f"al-{t.get('album_id')}",
            "isDir": False,
            "title": t.get("title") or "",
            "album": album.get("name") or "",
            "albumId": f"al-{t.get('album_id')}",
            "artist": artist.get("name") or "",
            "artistId": f"ar-{artist_id}" if artist_id else None,
            "coverArt": f"al-{t.get('album_id')}",
            "track": t.get("track_number") or 0,
            "year": album.get("year") or 0,
            "genre": t.get("genre") or None,
            "size": t.get("size_bytes") or 0,
            "contentType": _MIME.get("." + ext, "application/octet-stream") if ext
                           else "application/octet-stream",
            "suffix": ext or None,
            "duration": int(t.get("duration") or 0),
            "bitRate": t.get("bitrate") or 0,
            "path": self._display_path(path),
            "discNumber": t.get("disc_number") or 1,
            "created": _iso(t.get("created_at")),
            "type": "music",
            "isVideo": False,
        }
        return {k: v for k, v in song.items() if v is not None}

    def _display_path(self, path) -> str:
        """Library-relative path for the Child `path` attribute (clients
        display it; Navidrome shows the path under the music folder)."""
        if not path:
            return ""
        try:
            root = str(self.context.fs.music_dir)
            abs_path = os.path.abspath(str(path))
            if abs_path.startswith(root.rstrip(os.sep) + os.sep):
                return os.path.relpath(abs_path, root)
        except Exception:
            pass
        return os.path.basename(str(path))

    def _album(self, a: dict, artists_by_id: dict, song_count: int,
               duration: int) -> dict:
        """AlbumID3 shape (getArtist/getAlbum/getAlbumList2/search3)."""
        artist = artists_by_id.get(a.get("artist_id")) or {}
        return {
            "id": f"al-{a['id']}",
            "name": a.get("name") or "",
            "artist": artist.get("name") or "",
            "artistId": f"ar-{a['artist_id']}",
            "coverArt": f"al-{a['id']}",
            "songCount": song_count,
            "duration": duration,
            "playCount": 0,
            "created": _iso(a.get("created_at")),
            "year": a.get("year") or 0,
        }

    @staticmethod
    def _album_as_child(entry: dict) -> dict:
        """Add the Child-shape fields old clients expect when albums appear
        as directory children (getMusicDirectory / v1 getAlbumList)."""
        entry = dict(entry)
        entry["title"] = entry["name"]
        entry["album"] = entry["name"]
        entry["isDir"] = True
        return entry

    def _artist_entry(self, a: dict, album_count: int) -> dict:
        entry = {
            "id": f"ar-{a['id']}",
            "name": a.get("name") or "",
            "coverArt": f"ar-{a['id']}",
            "albumCount": album_count,
        }
        if a.get("image_url"):
            entry["artistImageUrl"] = a["image_url"]
        return entry

    # --------------------- AudioMuse-AI client -----------------------
    # Wire contract confirmed against AudioMuse-AI source (NeptuneHub/
    # AudioMuse-AI v3.5.1, app_ivf.py / app_artist_similarity.py / app_auth.py)
    # and its official Navidrome plugin (NeptuneHub/AudioMuse-AI-NV-plugin,
    # main.go): base URL http://<host>:8000, `Authorization: Bearer <API_TOKEN>`
    # (API token set in AudioMuse's Setup Wizard), and:
    #   GET /api/similar_tracks?item_id=<id> | title=<t>&artist=<a> &n=<n>
    #        -> bare JSON array of {item_id, title, author, album, distance, ...}
    #   GET /api/similar_artists?artist=<name>|artist_id=<id>&n=<n>
    #        -> bare JSON array of {artist, artist_id, divergence, ...}
    #   GET /api/health -> {"status": "ok"}  (no auth)
    # `item_id`/`artist_id` are the *media server's* ids in AudioMuse's own
    # id space (i.e. fnack's tr-/ar- ids when AudioMuse is pointed at fnack's
    # /rest API, Navidrome ids otherwise) — so results are matched back to
    # fnack tracks by id when possible, then by artist+title strings.
    def _audiomuse_enabled(self) -> bool:
        return self._bool_setting("audiomuse_enabled") and bool(self._setting("audiomuse_url").strip())

    def _audiomuse_get(self, path: str, params: dict) -> dict | list:
        """GET an AudioMuse-AI endpoint and return parsed JSON.

        Raises on any transport/HTTP/parsing problem — callers treat every
        exception as "integration unavailable" and fall back locally.
        """
        base = self._setting("audiomuse_url").strip().rstrip("/")
        headers = {}
        token = self._setting("audiomuse_api_token").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        server = self._setting("audiomuse_server").strip()
        if server:
            params = {**params, "server": server}
        resp = self.context.http.get(f"{base}{path}", params=params,
                                     headers=headers, timeout=8)
        resp.raise_for_status()
        return resp.json()

    def _audiomuse_similar_tracks(self, t: dict, count: int) -> list[dict]:
        """Ask AudioMuse-AI for songs similar to track `t`.

        Returns a list of fnack track dicts (matched back to fnack's library
        so clients get playable ids). Uses the title+artist lookup form, which
        works whether AudioMuse is pointed at fnack's own /rest API or at a
        Navidrome serving the same files.
        """
        data = self._audiomuse_get("/api/similar_tracks", {
            "title": t.get("title") or "",
            "artist": self._track_artist_name(t),
            "n": count,
            "eliminate_duplicates": "true",
        })
        items = data if isinstance(data, list) else (
            data.get("similar_tracks") or data.get("results")
            or data.get("tracks") or [])
        return self._match_tracks(items, count)

    def _audiomuse_similar_artists(self, artist_name: str, count: int) -> list[str]:
        """Ask AudioMuse-AI for artists similar to `artist_name`.

        Returns artist names when AudioMuse has data; empty when it doesn't
        (callers fall back locally on falsy results).
        """
        data = self._audiomuse_get("/api/similar_artists",
                                   {"artist": artist_name, "n": count})
        items = data if isinstance(data, list) else (
            data.get("similar_artists") or data.get("results") or [])
        names = []
        for item in items:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and item.get("artist"):
                names.append(str(item["artist"]))
            elif isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        return names

    # ------------------------ local fallbacks ------------------------
    def _track_artist_name(self, t: dict) -> str:
        for a in self._artists():
            if a["id"] == t.get("artist_id"):
                return a.get("name") or ""
        return ""

    def _match_tracks(self, items: list, count: int) -> list[dict]:
        """Match AudioMuse result items to fnack track dicts.

        AudioMuse echoes the media server's own item id back, so when it is
        pointed at fnack's /rest API the id is `tr-<n>` and matches directly;
        otherwise fall back to artist+title string matching.
        """
        downloaded = self._downloaded_tracks()
        by_id = {f"tr-{t['id']}": t for t in downloaded}
        tracks_by_artist: dict[int, dict[str, dict]] = {}
        for t in downloaded:
            key = (t.get("title") or "").strip().casefold()
            tracks_by_artist.setdefault(t.get("artist_id"), {})[key] = t
        artists_by_name = {}
        for a in self._artists():
            artists_by_name[(a.get("name") or "").strip().casefold()] = a["id"]
        matched, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                continue
            t = by_id.get(str(item.get("item_id") or ""))
            if t is None:
                artist_id = artists_by_name.get(str(
                    item.get("author") or item.get("artist")
                    or item.get("artist_name") or "").strip().casefold())
                title_key = str(item.get("title") or item.get("song")
                                or "").strip().casefold()
                if artist_id and title_key:
                    t = tracks_by_artist.get(artist_id, {}).get(title_key)
            if t and t["id"] not in seen:
                seen.add(t["id"])
                matched.append(t)
            if len(matched) >= count:
                break
        return matched

    def _fallback_similar_tracks(self, t: dict, count: int) -> list[dict]:
        """Without AudioMuse: other downloaded songs by the same artist."""
        pool = [x for x in self._downloaded_tracks()
                if x.get("artist_id") == t.get("artist_id") and x["id"] != t["id"]]
        random.shuffle(pool)
        return pool[:count]

    def _fallback_similar_artists(self, artist: dict, count: int) -> list[dict]:
        """Without AudioMuse: a sample of other artists in the library."""
        others = [a for a in self._artists() if a["id"] != artist.get("id")]
        random.shuffle(others)
        return others[:count]

    # ------------------------- route registration --------------------
    def register_routes(self, blueprint: Blueprint) -> None:
        bp = blueprint

        # -- OpenSubsonic ------------------------------------------------
        @bp.route("/rest/getOpenSubsonicExtensions", methods=["GET", "POST"])
        @bp.route("/rest/getOpenSubsonicExtensions.view", methods=["GET", "POST"])
        def open_subsonic_extensions():
            # OpenSubsonic requires this to be reachable without auth.
            # fnack implements the base OS envelope only — no extensions to
            # advertise (transcoding/similarity extensions don't apply here;
            # AudioMuse-AI enriches getSimilarSongs/getArtistInfo directly).
            return self._respond({"openSubsonicExtensions": []})

        # -- ping / license ------------------------------------------------
        @bp.route("/rest/ping", methods=["GET", "POST"])
        @bp.route("/rest/ping.view", methods=["GET", "POST"])
        @bp.route("/rest/getLicense", methods=["GET", "POST"])
        @bp.route("/rest/getLicense.view", methods=["GET", "POST"])
        def ping_license():
            guard = self._authorized()
            if guard:
                return guard
            if request.path.startswith("/rest/getLicense"):
                # XSD: `valid` is required; email/licenseExpires optional.
                return self._respond({"license": {
                    "valid": True,
                    "email": "fnack@localhost",
                    "licenseExpires": "2035-01-01T00:00:00.000Z",
                }})
            return self._respond()

        # -- music folders / indexes / artists -----------------------------
        @bp.route("/rest/getMusicFolders", methods=["GET", "POST"])
        @bp.route("/rest/getMusicFolders.view", methods=["GET", "POST"])
        def music_folders():
            guard = self._authorized()
            if guard:
                return guard
            return self._respond({"musicFolders": {"musicFolder": [
                # XSD types musicFolder.id as xs:int (unlike media ids).
                {"id": 0, "name": "fnack"}]}})

        @bp.route("/rest/getIndexes", methods=["GET", "POST"])
        @bp.route("/rest/getIndexes.view", methods=["GET", "POST"])
        def indexes():
            guard = self._authorized()
            if guard:
                return guard
            albums = self._albums()
            album_count = {}
            for a in albums:
                album_count[a["artist_id"]] = album_count.get(a["artist_id"], 0) + 1
            index: dict[str, list] = {}
            for a in self._artists():
                index.setdefault(_index_letter(a["name"]), []).append(
                    self._artist_entry(a, album_count.get(a["id"], 0)))
            return self._respond({"indexes": {
                "lastModified": _STARTED_AT_MS,
                "ignoredArticles": IGNORED_ARTICLES,
                "index": [{"name": k, "artist": v} for k, v in sorted(index.items())],
            }})

        @bp.route("/rest/getArtists", methods=["GET", "POST"])
        @bp.route("/rest/getArtists.view", methods=["GET", "POST"])
        def get_artists():
            guard = self._authorized()
            if guard:
                return guard
            albums = self._albums()
            album_count = {}
            for a in albums:
                album_count[a["artist_id"]] = album_count.get(a["artist_id"], 0) + 1
            index: dict[str, list] = {}
            for a in self._artists():
                index.setdefault(_index_letter(a["name"]), []).append(
                    self._artist_entry(a, album_count.get(a["id"], 0)))
            return self._respond({"artists": {
                "ignoredArticles": IGNORED_ARTICLES,
                "index": [{"name": k, "artist": v} for k, v in sorted(index.items())],
            }})

        @bp.route("/rest/getArtist", methods=["GET", "POST"])
        @bp.route("/rest/getArtist.view", methods=["GET", "POST"])
        def get_artist():
            guard = self._authorized() or self._missing_id()
            if guard:
                return guard
            artist = self._resolve("ar-")
            if not artist:
                return self._err(70, "Artist not found")
            artists_by_id = {a["id"]: a for a in self._artists()}
            albums = [a for a in self._albums() if a["artist_id"] == artist["id"]]
            tracks = self._downloaded_tracks()
            songs_per_album: dict[int, int] = {}
            dur_per_album: dict[int, int] = {}
            for t in tracks:
                songs_per_album[t["album_id"]] = songs_per_album.get(t["album_id"], 0) + 1
                dur_per_album[t["album_id"]] = (dur_per_album.get(t["album_id"], 0)
                                                + int(t.get("duration") or 0))
            album_entries = []
            for a in sorted(albums, key=lambda x: ((x.get("year") or 0), x["name"])):
                album_entries.append(self._album(a, artists_by_id,
                                                 songs_per_album.get(a["id"], 0),
                                                 dur_per_album.get(a["id"], 0)))
            return self._respond({"artist": {
                **self._artist_entry(artist, len(albums)),
                "album": album_entries,
            }})

        # -- directory browsing (v1-style tree) ----------------------------
        @bp.route("/rest/getMusicDirectory", methods=["GET", "POST"])
        @bp.route("/rest/getMusicDirectory.view", methods=["GET", "POST"])
        def music_directory():
            guard = self._authorized() or self._missing_id()
            if guard:
                return guard
            rid = (request.values.get("id") or "").strip()
            artists_by_id = {a["id"]: a for a in self._artists()}
            if rid in ("0", "1"):  # the (single) music folder root
                children = []
                for a in self._artists():
                    children.append({**self._artist_entry(a, 0), "parent": "0",
                                     "isDir": True, "title": a.get("name") or ""})
                return self._respond({"directory": {
                    "id": "0", "name": "fnack", "child": children}})
            if rid.startswith("ar-"):
                artist = self._resolve("ar-")
                if not artist:
                    return self._err(70, "Artist not found")
                albums = [a for a in self._albums() if a["artist_id"] == artist["id"]]
                children = []
                for a in albums:
                    child = self._album_as_child(self._album(
                        a, artists_by_id, 0, 0))
                    child["parent"] = f"ar-{artist['id']}"
                    children.append(child)
                return self._respond({"directory": {
                    "id": f"ar-{artist['id']}", "name": artist.get("name") or "",
                    "child": children}})
            if rid.startswith("al-"):
                album = self._resolve("al-")
                if not album:
                    return self._err(70, "Album not found")
                albums_by_id = {a["id"]: a for a in self._albums()}
                tracks = [t for t in self._downloaded_tracks()
                          if t["album_id"] == album["id"]]
                children = [self._song(t, albums_by_id, artists_by_id,
                                       parent=f"al-{album['id']}")
                            for t in tracks]
                return self._respond({"directory": {
                    "id": f"al-{album['id']}", "name": album.get("name") or "",
                    "parent": f"ar-{album['artist_id']}",
                    "child": children}})
            return self._err(70, "Directory not found")

        # -- album / song ---------------------------------------------------
        @bp.route("/rest/getAlbum", methods=["GET", "POST"])
        @bp.route("/rest/getAlbum.view", methods=["GET", "POST"])
        def get_album():
            guard = self._authorized() or self._missing_id()
            if guard:
                return guard
            album = self._resolve("al-")
            if not album:
                return self._err(70, "Album not found")
            artists_by_id = {a["id"]: a for a in self._artists()}
            albums_by_id = {a["id"]: a for a in self._albums()}
            tracks = [t for t in self._downloaded_tracks()
                      if t["album_id"] == album["id"]]
            songs = [self._song(t, albums_by_id, artists_by_id) for t in tracks]
            entry = self._album(album, artists_by_id, len(songs),
                                sum(int(t.get("duration") or 0) for t in tracks))
            genres = sorted({t.get("genre") for t in tracks if t.get("genre")})
            if genres:
                entry["genre"] = ", ".join(genres)
            entry["song"] = songs
            return self._respond({"album": entry})

        @bp.route("/rest/getSong", methods=["GET", "POST"])
        @bp.route("/rest/getSong.view", methods=["GET", "POST"])
        def get_song():
            guard = self._authorized() or self._missing_id()
            if guard:
                return guard
            t = self._resolve("tr-")
            if not t:
                return self._err(70, "Song not found")
            albums_by_id = {a["id"]: a for a in self._albums()}
            artists_by_id = {a["id"]: a for a in self._artists()}
            return self._respond({"song": self._song(t, albums_by_id, artists_by_id)})

        # -- album lists ----------------------------------------------------
        @bp.route("/rest/getAlbumList2", methods=["GET", "POST"])
        @bp.route("/rest/getAlbumList2.view", methods=["GET", "POST"])
        def album_list2():
            return self._album_list("albumList2", with_parent=False)

        @bp.route("/rest/getAlbumList", methods=["GET", "POST"])
        @bp.route("/rest/getAlbumList.view", methods=["GET", "POST"])
        def album_list():
            return self._album_list("albumList", with_parent=True)

        # -- search ----------------------------------------------------------
        @bp.route("/rest/search3", methods=["GET", "POST"])
        @bp.route("/rest/search3.view", methods=["GET", "POST"])
        def search3():
            return self._search("searchResult3")

        @bp.route("/rest/search2", methods=["GET", "POST"])
        @bp.route("/rest/search2.view", methods=["GET", "POST"])
        def search2():
            return self._search("searchResult2")

        # -- similarity / artist info ---------------------------------------
        @bp.route("/rest/getSimilarSongs2", methods=["GET", "POST"])
        @bp.route("/rest/getSimilarSongs2.view", methods=["GET", "POST"])
        def similar_songs2():
            return self._similar_songs("similarSongs2")

        @bp.route("/rest/getSimilarSongs", methods=["GET", "POST"])
        @bp.route("/rest/getSimilarSongs.view", methods=["GET", "POST"])
        def similar_songs():
            return self._similar_songs("similarSongs")

        @bp.route("/rest/getArtistInfo2", methods=["GET", "POST"])
        @bp.route("/rest/getArtistInfo2.view", methods=["GET", "POST"])
        def artist_info2():
            return self._artist_info("artistInfo2")

        @bp.route("/rest/getArtistInfo", methods=["GET", "POST"])
        @bp.route("/rest/getArtistInfo.view", methods=["GET", "POST"])
        def artist_info():
            return self._artist_info("artistInfo")

        # -- genres / random / starred ---------------------------------------
        @bp.route("/rest/getGenres", methods=["GET", "POST"])
        @bp.route("/rest/getGenres.view", methods=["GET", "POST"])
        def genres():
            guard = self._authorized()
            if guard:
                return guard
            counts: dict[str, dict] = {}
            albums_by_id = {a["id"]: a for a in self._albums()}
            for t in self._downloaded_tracks():
                genre = (t.get("genre") or "").strip()
                if not genre:
                    continue
                bucket = counts.setdefault(genre, {"songCount": 0, "albums": set()})
                bucket["songCount"] += 1
                bucket["albums"].add(t.get("album_id"))
            return self._respond({"genres": {"genre": [
                {"songCount": b["songCount"], "albumCount": len(b["albums"]),
                 "value": g}
                for g, b in sorted(counts.items(), key=lambda kv: kv[0].casefold())
            ]}})

        @bp.route("/rest/getRandomSongs", methods=["GET", "POST"])
        @bp.route("/rest/getRandomSongs.view", methods=["GET", "POST"])
        def random_songs():
            guard = self._authorized()
            if guard:
                return guard
            size = self._int_param("size", 10, maxv=500)
            tracks = self._downloaded_tracks()
            genre = (request.values.get("genre") or "").strip()
            from_year = self._int_param("fromYear", 0, maxv=9999)
            to_year = self._int_param("toYear", 0, maxv=9999)
            albums_by_id = {a["id"]: a for a in self._albums()}
            if genre:
                g = genre.casefold()
                tracks = [t for t in tracks
                          if (t.get("genre") or "").casefold() == g]
            if from_year or to_year:
                def in_range(t):
                    year = (albums_by_id.get(t.get("album_id")) or {}).get("year") or 0
                    if from_year and year < from_year:
                        return False
                    if to_year and year > to_year:
                        return False
                    return True
                tracks = [t for t in tracks if in_range(t)]
            random.shuffle(tracks)
            artists_by_id = {a["id"]: a for a in self._artists()}
            return self._respond({"randomSongs": {"song": [
                self._song(t, albums_by_id, artists_by_id)
                for t in tracks[:size]]}})

        @bp.route("/rest/getStarred", methods=["GET", "POST"])
        @bp.route("/rest/getStarred.view", methods=["GET", "POST"])
        @bp.route("/rest/getStarred2", methods=["GET", "POST"])
        @bp.route("/rest/getStarred2.view", methods=["GET", "POST"])
        def starred():
            guard = self._authorized()
            if guard:
                return guard
            key = "starred2" if request.path.startswith("/rest/getStarred2") else "starred"
            return self._respond({key: {}})

        @bp.route("/rest/star", methods=["GET", "POST"])
        @bp.route("/rest/star.view", methods=["GET", "POST"])
        @bp.route("/rest/unstar", methods=["GET", "POST"])
        @bp.route("/rest/unstar.view", methods=["GET", "POST"])
        def star():
            guard = self._authorized()
            if guard:
                return guard
            # fnack has no persistent favourites; accept and no-op so clients
            # don't surface errors.
            return self._respond()

        @bp.route("/rest/getPlaylists", methods=["GET", "POST"])
        @bp.route("/rest/getPlaylists.view", methods=["GET", "POST"])
        def playlists():
            guard = self._authorized()
            if guard:
                return guard
            return self._respond({"playlists": {"playlist": []}})

        @bp.route("/rest/getVideos", methods=["GET", "POST"])
        @bp.route("/rest/getVideos.view", methods=["GET", "POST"])
        def videos():
            guard = self._authorized()
            if guard:
                return guard
            return self._respond({"videos": {"video": []}})

        # -- media delivery ---------------------------------------------------
        @bp.route("/rest/stream", methods=["GET", "POST"])
        @bp.route("/rest/stream.view", methods=["GET", "POST"])
        @bp.route("/rest/download", methods=["GET", "POST"])
        @bp.route("/rest/download.view", methods=["GET", "POST"])
        def stream():
            guard = self._authorized() or self._missing_id()
            if guard:
                return guard
            t = self._resolve("tr-")
            if not t:
                return self._xml_error(70, "Song not found")
            path = self._track_stream_path(t)
            if not path:
                return self._xml_error(70, "File not found")
            ext = os.path.splitext(path)[1].lower()
            # maxBitRate/format/transcode params are accepted but ignored:
            # fnack's container ships no ffmpeg, so files stream as-is.
            return send_file(path, mimetype=_MIME.get(ext, "application/octet-stream"))

        @bp.route("/rest/getCoverArt", methods=["GET", "POST"])
        @bp.route("/rest/getCoverArt.view", methods=["GET", "POST"])
        def cover():
            guard = self._authorized() or self._missing_id()
            if guard:
                return guard
            rid = (request.values.get("id") or "").strip()
            # `size` (max edge in px) is accepted but not applied — no image
            # library in the container; clients scale down client-side.
            if rid.startswith("al-"):
                album = self._resolve("al-")
                if album:
                    served = self._local_cover(album.get("local_path"))
                    if served:
                        return served
                    if album.get("cover_url"):
                        proxied = self._proxy_image(album["cover_url"])
                        if proxied:
                            return proxied
            elif rid.startswith("ar-"):
                artist = self._resolve("ar-")
                if artist and artist.get("image_url"):
                    proxied = self._proxy_image(artist["image_url"])
                    if proxied:
                        return proxied
            return self._xml_error(70, "Cover not found")

        # -- scan status -------------------------------------------------------
        @bp.route("/rest/getScanStatus", methods=["GET", "POST"])
        @bp.route("/rest/getScanStatus.view", methods=["GET", "POST"])
        def scan_status():
            guard = self._authorized()
            if guard:
                return guard
            return self._respond({"scanStatus": {
                "scanning": False, "count": len(self._tracks())}})

        @bp.route("/rest/startScan", methods=["GET", "POST"])
        @bp.route("/rest/startScan.view", methods=["GET", "POST"])
        def start_scan():
            guard = self._authorized()
            if guard:
                return guard
            # fnack's library is DB-driven (folder watcher + import do the
            # scanning); emit the core event so watchers can react.
            try:
                self.context.events.emit("library.scan_requested")
            except Exception:
                pass
            return self._respond({"scanStatus": {
                "scanning": False, "count": len(self._tracks())}})

    # ------------------------------------------------------------------
    # Helpers used by route closures above
    # ------------------------------------------------------------------
    def _resolve(self, prefix: str):
        """Fetch an artist/album/track from an `ar-/al-/tr-` id param."""
        rid = (request.values.get("id") or "").strip()
        if not rid.startswith(prefix):
            return None
        try:
            numeric = int(rid[len(prefix):])
        except ValueError:
            return None
        lib = self.context.library
        if prefix == "ar-":
            return lib.get_artist(numeric)
        if prefix == "al-":
            return lib.get_album(numeric)
        if prefix == "tr-":
            return lib.get_track(numeric)
        return None

    def _missing_id(self):
        """Error response when the `id` param is absent (spec code 10), or
        None when present (unknown ids still resolve to code 70 later)."""
        if not (request.values.get("id") or "").strip():
            return self._err(10, "Required parameter 'id' is missing")
        return None

    def _int_param(self, name: str, default: int, maxv: int | None = None) -> int:
        try:
            val = int(request.values.get(name) or default)
        except (TypeError, ValueError):
            return default
        if val < 0:
            val = default
        if maxv is not None:
            val = min(val, maxv)
        return val

    def _local_cover(self, album_dir) -> Response | None:
        if not album_dir:
            return None
        # Honour the user's configured cover filename first, then defaults.
        names = []
        configured = ""
        try:
            configured = (self.context.library.get_setting("cover_art_filename", "")
                          or "").strip()
        except Exception:
            configured = ""
        if configured:
            names.append(configured)
        names.extend(_COVER_NAMES)
        for name in names:
            path = os.path.join(str(album_dir), name)
            if os.path.isfile(path):
                ext = os.path.splitext(path)[1].lower()
                return send_file(path, mimetype=_COVER_MIME.get(ext, "image/jpeg"))
        return None

    def _proxy_image(self, url: str) -> Response | None:
        try:
            resp = self.context.http.get(url, timeout=10)
            if resp.status_code != 200:
                return None
            ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            if not ctype.startswith("image/"):
                ctype = "image/jpeg"
            return Response(resp.content, mimetype=ctype)
        except Exception as exc:
            self.context.log.warning("Cover art proxy failed for %s: %s", url, exc)
            return None

    def _album_list(self, key: str, with_parent: bool):
        """getAlbumList / getAlbumList2 with type/size/offset handling."""
        guard = self._authorized()
        if guard:
            return guard
        kind = (request.values.get("type") or "alphabeticalByName").strip()
        size = self._int_param("size", 10, maxv=500)
        offset = self._int_param("offset", 0, maxv=10**9)
        artists_by_id = {a["id"]: a for a in self._artists()}
        tracks = self._downloaded_tracks()
        songs_per_album: dict[int, int] = {}
        dur_per_album: dict[int, int] = {}
        for t in tracks:
            songs_per_album[t["album_id"]] = songs_per_album.get(t["album_id"], 0) + 1
            dur_per_album[t["album_id"]] = (dur_per_album.get(t["album_id"], 0)
                                            + int(t.get("duration") or 0))
        albums = [a for a in self._albums() if songs_per_album.get(a["id"], 0) > 0]
        if kind == "alphabeticalByName":
            albums.sort(key=lambda a: (a.get("name") or "").casefold())
        elif kind == "alphabeticalByArtist":
            albums.sort(key=lambda a: ((artists_by_id.get(a["artist_id"]) or {})
                                       .get("name") or "").casefold())
        elif kind == "newest":
            albums.sort(key=lambda a: _iso(a.get("created_at")), reverse=True)
        elif kind == "byYear":
            lo = self._int_param("fromYear", 0, maxv=9999)
            hi = self._int_param("toYear", 0, maxv=9999)
            if lo and hi:  # a reversed range still selects the same years
                lo_b, hi_b = min(lo, hi), max(lo, hi)
            else:
                lo_b, hi_b = lo, hi
            if lo_b:
                albums = [a for a in albums if (a.get("year") or 0) >= lo_b]
            if hi_b:
                albums = [a for a in albums if (a.get("year") or 0) <= hi_b]
            # Only a reversed range (fromYear > toYear) sorts newest-first;
            # that's the spec'd convention clients use for "descending".
            newest_first = bool(lo and hi and lo > hi)
            albums.sort(key=lambda a: (a.get("year") or 0, a.get("name") or ""),
                        reverse=newest_first)
        elif kind == "byGenre":
            genre = (request.values.get("genre") or "").strip().casefold()
            album_ids = {t.get("album_id") for t in tracks
                         if (t.get("genre") or "").casefold() == genre}
            albums = [a for a in albums if a["id"] in album_ids]
            albums.sort(key=lambda a: (a.get("name") or "").casefold())
        elif kind == "random":
            random.shuffle(albums)
        else:
            # highest / frequent / recent / starred: fnack tracks no play
            # counts or favourites — degrade to a stable alphabetical list.
            albums.sort(key=lambda a: (a.get("name") or "").casefold())
        albums = albums[offset:offset + size]
        entries = []
        for a in albums:
            entry = self._album(a, artists_by_id, songs_per_album.get(a["id"], 0),
                                dur_per_album.get(a["id"], 0))
            if with_parent:
                entry = self._album_as_child(entry)
                entry["parent"] = f"ar-{a['artist_id']}"
            entries.append(entry)
        return self._respond({key: {"album": entries}})

    def _search(self, key: str):
        guard = self._authorized()
        if guard:
            return guard
        query = (request.values.get("query") or "").strip()
        if query.startswith('"') and query.endswith('"') and len(query) > 1:
            query = query[1:-1]  # clients quote exact matches
        artist_count = self._int_param("artistCount", 20, maxv=500)
        album_count = self._int_param("albumCount", 20, maxv=500)
        song_count = self._int_param("songCount", 20, maxv=500)
        artist_offset = self._int_param("artistOffset", 0, maxv=10**9)
        album_offset = self._int_param("albumOffset", 0, maxv=10**9)
        song_offset = self._int_param("songOffset", 0, maxv=10**9)
        # Empty query lists the library from the top (Navidrome behaviour —
        # some clients use search3 as their "browse everything" entry point).
        if query:
            result = self.context.library.search_library(
                query, artist_limit=artist_count + artist_offset,
                album_limit=album_count + album_offset,
                track_limit=song_count + song_offset)
        else:
            result = {"artists": self._artists(), "albums": self._albums(),
                      "tracks": self._downloaded_tracks()}
        artists_by_id = {a["id"]: a for a in self._artists()}
        albums_by_id = {a["id"]: a for a in self._albums()}
        album_songs: dict[int, int] = {}
        for t in self._downloaded_tracks():
            album_songs[t["album_id"]] = album_songs.get(t["album_id"], 0) + 1
        artist_entries = [
            self._artist_entry(a, album_songs.get(a["id"], 0))
            for a in result["artists"][artist_offset:artist_offset + artist_count]]
        album_entries = []
        for a in result["albums"][album_offset:album_offset + album_count]:
            if album_songs.get(a["id"], 0) <= 0:
                continue  # nothing playable — keep clients away from dead ends
            album_entries.append(self._album(a, artists_by_id,
                                             album_songs.get(a["id"], 0), 0))
        track_entries = [
            self._song(t, albums_by_id, artists_by_id)
            for t in result["tracks"][song_offset:song_offset + song_count]
            if t.get("is_downloaded")]
        return self._respond({key: {
            "artist": artist_entries, "album": album_entries,
            "song": track_entries}})

    def _similar_songs(self, key: str):
        guard = self._authorized() or self._missing_id()
        if guard:
            return guard
        count = self._int_param("count", 50, maxv=500)
        seed = self._resolve("tr-")
        if not seed:
            return self._err(70, "Song not found")
        tracks = []
        if self._audiomuse_enabled():
            try:
                tracks = self._audiomuse_similar_tracks(seed, count)
            except Exception as exc:
                self.context.log.warning("AudioMuse similar-tracks failed, "
                                         "using local fallback: %s", exc)
        if not tracks:
            tracks = self._fallback_similar_tracks(seed, count)
        albums_by_id = {a["id"]: a for a in self._albums()}
        artists_by_id = {a["id"]: a for a in self._artists()}
        songs = [self._song(t, albums_by_id, artists_by_id) for t in tracks[:count]]
        return self._respond({key: {"similarSong": songs}})

    def _artist_info(self, key: str):
        guard = self._authorized() or self._missing_id()
        if guard:
            return guard
        count = self._int_param("count", 20, maxv=100)
        artist = self._resolve("ar-")
        if not artist:
            return self._err(70, "Artist not found")
        info = {"biography": "", "musicBrainzId": "", "lastFmUrl": ""}
        if artist.get("image_url"):
            for size_key in ("smallImageUrl", "mediumImageUrl", "largeImageUrl"):
                info[size_key] = artist["image_url"]
        names = []
        if self._audiomuse_enabled():
            try:
                names = self._audiomuse_similar_artists(artist.get("name") or "", count)
            except Exception as exc:
                self.context.log.warning("AudioMuse similar-artists failed, "
                                         "using local fallback: %s", exc)
        artists_by_name = {}
        if names:
            for a in self._artists():
                artists_by_name[(a.get("name") or "").casefold()] = {
                    "id": f"ar-{a['id']}", "name": a.get("name") or "",
                    "coverArt": f"ar-{a['id']}"}
        # Artists AudioMuse knows but fnack doesn't stock get the "-1" id
        # convention used by official Subsonic and Navidrome for
        # not-present similar artists.
        fallback_entry = {"id": "-1", "name": ""}
        if names:
            similar = []
            for n in names:
                if len(similar) >= count:
                    break
                entry = artists_by_name.get(n.casefold())
                if entry:
                    similar.append(dict(entry))
                else:
                    similar.append({**fallback_entry, "name": n})
        else:
            similar = []
            for a in self._fallback_similar_artists(artist, count):
                similar.append({"id": f"ar-{a['id']}", "name": a.get("name") or "",
                                "coverArt": f"ar-{a['id']}"})
        info["similarArtist"] = similar[:count]
        return self._respond({key: info})
