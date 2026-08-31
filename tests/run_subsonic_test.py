"""Live-HTTP integration test for the fnack.subsonic plugin.

Boots a real Flask app (real models, real plugin manager, the real bundled
fnack.subsonic plugin), serves it on a local port, and drives it with actual
HTTP requests — the way Subsonic clients talk to it. Also spins a mock
AudioMuse-AI instance (same wire contract as NeptuneHub/AudioMuse-AI v3.5.1:
GET /api/similar_tracks, GET /api/similar_artists, Bearer auth) to verify the
integration end-to-end, including the "AudioMuse disabled / unreachable"
fallback paths.

Run from the repo root:

    .venv/bin/python tests/run_subsonic_test.py

Expected output ends with `SUBSONIC TEST PASSED`.
"""

import hashlib
import os
import sys
import tempfile
import threading
from pathlib import Path

import requests
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, request

from models import Album, AppSetting, Artist, Track, db

FAILED = []


def check(name: str, cond: bool, detail: str = ""):
    status = "ok  " if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# App + schema
# ---------------------------------------------------------------------------

tmp = tempfile.mkdtemp(prefix="fnack-subsonic-test-")
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp}/library.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

with app.app_context():
    import plugins.models  # noqa: F401 — registers the plugin tables with `db`
    db.create_all()

    # --- seed a small library -------------------------------------------
    aurora = Artist(spotify_id="seed-ar-1", name="Aurora")
    beatles = Artist(spotify_id="seed-ar-2", name="The Beatles")
    edith = Artist(spotify_id="seed-ar-3", name="Édith Piaf")
    db.session.add_all([aurora, beatles, edith])
    db.session.flush()

    hits = Album(artist_id=aurora.id, name="Hits & Rarities <Live>", year=2021,
                 cover_url="http://127.0.0.1:1/should-fail.jpg",
                 local_path=tmp, is_downloaded=True)
    waves = Album(artist_id=aurora.id, name="Waves", year=2019, is_downloaded=True)
    abbey = Album(artist_id=beatles.id, name="Abbey Road", year=1969,
                  is_downloaded=True)
    db.session.add_all([hits, waves, abbey])
    db.session.flush()

    # Real files on disk so stream/getCoverArt return real bytes.
    audio_path = Path(tmp) / "aurora - runaway.flac"
    audio_path.write_bytes(b"\x00\x01fLaC-AUDIO-BYTES-\xff\xfe")
    audio2_path = Path(tmp) / "aurora - through the eyes.mp3"
    audio2_path.write_bytes(b"ID3-MP3-AUDIO-BYTES")
    audio3_path = Path(tmp) / "beatles - here comes the sun.mp3"
    audio3_path.write_bytes(b"ID3-MP3-BEATLES")
    cover_path = Path(tmp) / "cover.jpg"
    cover_path.write_bytes(b"\xff\xd8\xff\xe0FAKEJPEG")

    tracks = [
        Track(album_id=hits.id, artist_id=aurora.id, title="Runaway",
              track_number=1, disc_number=1, duration=245.4, bitrate=996,
              size_bytes=len(audio_path.read_bytes()), status="completed",
              is_downloaded=True, genre="Dream Pop", local_path=str(audio_path),
              file_path=str(audio_path)),
        Track(album_id=hits.id, artist_id=aurora.id, title="Through the Eyes",
              track_number=2, disc_number=1, duration=201.0, bitrate=320,
              size_bytes=len(audio2_path.read_bytes()), status="completed",
              is_downloaded=True, genre="Dream Pop", local_path=str(audio2_path)),
        # Present in the DB but never downloaded — must NOT surface as a
        # playable song anywhere.
        Track(album_id=hits.id, artist_id=aurora.id, title="Not Yet Downloaded",
              track_number=3, disc_number=1, duration=180.0, status="missing",
              is_downloaded=False, local_path=None),
        Track(album_id=waves.id, artist_id=aurora.id, title="Echoes",
              track_number=1, disc_number=1, duration=190.2, bitrate=320,
              size_bytes=1024, status="completed", is_downloaded=True,
              genre="Synthwave", local_path=None),  # downloaded row, file gone
        Track(album_id=abbey.id, artist_id=beatles.id, title="Here Comes the Sun",
              track_number=7, disc_number=1, duration=185.0, bitrate=256,
              size_bytes=len(audio3_path.read_bytes()), status="completed",
              is_downloaded=True, genre="Classic Rock", local_path=str(audio3_path)),
    ]
    db.session.add_all(tracks)
    db.session.add(AppSetting(key="save_cover_art", value="true"))
    db.session.add(AppSetting(key="cover_art_filename", value="cover.jpg"))
    db.session.commit()

    AURORA_ID, BEATLES_ID, EDITH_ID = aurora.id, beatles.id, edith.id
    HITS_ID, WAVES_ID, ABBEY_ID = hits.id, waves.id, abbey.id
    T_RUNAWAY = tracks[0].id
    T_ECHOES = tracks[3].id  # is_downloaded but file missing
    T_MISSING = tracks[2].id

# ---------------------------------------------------------------------------
# Plugin loading (same path app.py uses) + route registration
# ---------------------------------------------------------------------------

with app.app_context():
    from plugins.manager import init_plugin_manager

    repo_root = Path(__file__).resolve().parent.parent
    manager = init_plugin_manager(
        plugins_dir=os.path.join(tmp, "no-user-plugins"),
        core_version="0.3.1",
        bundled_plugins_dir=str(repo_root / "bundled_plugins"),
    )
    manager.load_all()  # enabled_ids=None → everything enabled (test mode)

    from plugins.base import ServerExtensionPlugin

    for loaded in manager._plugins.values():  # noqa: SLF001 — mirrors app.py
        if loaded.enabled and isinstance(loaded.instance, ServerExtensionPlugin):
            from flask import Blueprint
            bp = Blueprint(
                f"plugin_{loaded.manifest.id.replace('.', '_').replace('-', '_')}",
                __name__, url_prefix="")
            loaded.instance.register_routes(bp)
            app.register_blueprint(bp)

    def set_setting(key: str, value: str):
        sub = manager._plugins.get("fnack.subsonic")
        sub.instance.context.settings.set(key, value)

# ---------------------------------------------------------------------------
# Live HTTP server + mock AudioMuse-AI
# ---------------------------------------------------------------------------

server = make_server("127.0.0.1", 0, app, threaded=True)
BASE = f"http://127.0.0.1:{server.server_port}"
threading.Thread(target=server.serve_forever, daemon=True).start()

mock = Flask("mock-audiomuse")
MOCK_TOKEN = "test-api-token"
MOCK_SEEN_AUTH = []
MOCK_SEEN_PARAMS = {}


@mock.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@mock.get("/api/similar_tracks")
def similar_tracks():
    MOCK_SEEN_AUTH.append(request.headers.get("Authorization"))
    MOCK_SEEN_PARAMS.update(request.args.to_dict())
    if request.headers.get("Authorization") != f"Bearer {MOCK_TOKEN}":
        return jsonify({"error": "Unauthorized"}), 401
    # Real v3.5.1 response shape: a bare JSON array of
    # {item_id, title, author, album, distance}.
    return jsonify([
        {"item_id": f"tr-{T_ECHOES}", "title": "Echoes", "author": "Aurora",
         "album": "Waves", "distance": 0.11},
        {"item_id": "navidrome-1234", "title": "Here Comes the Sun",
         "author": "The Beatles", "album": "Abbey Road", "distance": 0.42},
        {"item_id": "navidrome-9999", "title": "Unknown Song",
         "author": "Unknown Artist", "album": "Unknown", "distance": 0.5},
    ])


@mock.get("/api/similar_artists")
def similar_artists():
    MOCK_SEEN_AUTH.append(request.headers.get("Authorization"))
    MOCK_SEEN_PARAMS.update(request.args.to_dict())
    if request.headers.get("Authorization") != f"Bearer {MOCK_TOKEN}":
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify([
        {"artist": "The Beatles", "artist_id": "nav-1", "divergence": 0.2},
        {"artist": "Ghost Artist", "artist_id": "nav-2", "divergence": 0.3},
    ])


mock_server = make_server("127.0.0.1", 0, mock, threaded=True)
MOCK_URL = f"http://127.0.0.1:{mock_server.server_port}"
threading.Thread(target=mock_server.serve_forever, daemon=True).start()


def get(path, params=None, headers=None):
    return requests.get(f"{BASE}{path}", params=params or {}, headers=headers or {},
                        timeout=10)


import xml.etree.ElementTree as ET  # noqa: E402


def subsonic_json(params):
    p = dict(params or {})
    p["f"] = "json"
    r = get("/rest/ping.view", p)
    return r


print(f"fnack under test: {BASE}")
print(f"mock AudioMuse-AI: {MOCK_URL}\n")

# ---------------------------------------------------------------------------
# 1. ping / auth
# ---------------------------------------------------------------------------
print("ping + auth")

r = get("/rest/ping.view")
check("ping.view default is XML", r.headers["Content-Type"].startswith("text/xml"),
      r.headers["Content-Type"])
root = ET.fromstring(r.content)
check("XML parses with ok status", root.tag == "subsonic-response"
      and root.get("status") == "ok" and root.get("version") == "1.16.1")
check("OpenSubsonic envelope attrs", root.get("type") == "fnack"
      and root.get("serverVersion") == "0.3.1" and root.get("openSubsonic") in ("1", "true"))

r = get("/rest/ping", {"f": "json"})
check("f=json returns application/json", r.headers["Content-Type"].startswith("application/json"))
body = r.json()["subsonic-response"]
check("JSON envelope ok", body["status"] == "ok" and body["version"] == "1.16.1"
      and body["openSubsonic"] is True)

r = get("/rest/ping", {"f": "jsonp", "callback": "cb"})
check("f=jsonp wraps in callback", r.text.startswith("cb("))

r = get("/rest/getLicense", {"f": "json"})
lic = r.json()["subsonic-response"]["license"]
check("getLicense valid=true (required attr)", lic["valid"] is True)

r = get("/rest/getOpenSubsonicExtensions", {"f": "json"})  # no credentials on purpose
ose = r.json()["subsonic-response"]
check("getOpenSubsonicExtensions reachable without auth, no error",
      r.status_code == 200 and ose["status"] == "ok" and "error" not in ose
      and ose.get("openSubsonicExtensions", []) == [])  # no extensions advertised

r = get("/rest/ping", {"u": "admin", "p": "nope", "f": "json"})
check("ping without key + wrong password still open (zero-auth)",
      r.json()["subsonic-response"]["status"] == "ok")

with app.app_context():
    set_setting  # noqa: B018 — keep the helper referenced
    from models import db as _db
    _db.session.add(AppSetting(key="api_key", value="sekrit"))
    _db.session.commit()

r = get("/rest/ping.view", {"f": "json"})
check("no creds + api key set → error 40",
      r.json()["subsonic-response"]["error"]["code"] == 40)

r = get("/rest/ping", {"u": "admin", "p": "sekrit", "f": "json"})
check("plaintext p= auth ok", r.json()["subsonic-response"]["status"] == "ok")

enc = "sekrit".encode().hex()
r = get("/rest/ping", {"u": "admin", "p": f"enc:{enc}", "f": "json"})
check("hex-encoded p=enc: auth ok", r.json()["subsonic-response"]["status"] == "ok")

salt = "abc123"
token = hashlib.md5(f"sekrit{salt}".encode()).hexdigest()
r = get("/rest/ping", {"u": "admin", "t": token, "s": salt, "f": "json"})
check("token t=md5(pw+salt) auth ok", r.json()["subsonic-response"]["status"] == "ok")

r = get("/rest/ping", {"u": "admin", "t": "deadbeef", "s": salt, "f": "json"})
check("bad token → error 40", r.json()["subsonic-response"]["error"]["code"] == 40)

r = get("/rest/ping", {"apiKey": "sekrit", "f": "json"})
check("OpenSubsonic apiKey= auth ok", r.json()["subsonic-response"]["status"] == "ok")

with app.app_context():
    from models import db as _db2
    row = _db2.session.get(AppSetting, "api_key")
    _db2.session.delete(row)
    _db2.session.commit()

# ---------------------------------------------------------------------------
# 2. Browsing: music folders, indexes, artists, directories, albums
# ---------------------------------------------------------------------------
print("browsing")

r = get("/rest/getMusicFolders", {"f": "json"})
mf = r.json()["subsonic-response"]["musicFolders"]["musicFolder"]
check("getMusicFolders single folder (xs:int id, always array)",
      isinstance(mf, list) and mf[0]["id"] == 0 and mf[0]["name"] == "fnack")

r = get("/rest/getArtists", {"f": "json"})
resp = r.json()["subsonic-response"]["artists"]
index = {i["name"]: i["artist"] for i in resp["index"]}
check("getArtists indexes 'A' with Aurora",
      any(a["name"] == "Aurora" for a in index.get("A", [])))
check("getArtists article-strip: The Beatles under B",
      any(a["name"] == "The Beatles" for a in index.get("B", [])))
check("getArtists unicode Édith under E",
      any(a["name"] == "Édith Piaf" for a in index.get("E", [])))
aurora_entry = next(a for i in resp["index"] for a in i["artist"]
                    if a["name"] == "Aurora")
check("artist entry has coverArt + albumCount",
      aurora_entry.get("coverArt") == f"ar-{AURORA_ID}" and aurora_entry["albumCount"] == 2)

r = get("/rest/getIndexes", {"f": "json"})
check("getIndexes same grouping",
      any(a["name"] == "Aurora" for i in r.json()["subsonic-response"]["indexes"]["index"]
          for a in i["artist"]))

r = get("/rest/getArtist", {"id": f"ar-{AURORA_ID}", "f": "json"})
artist = r.json()["subsonic-response"]["artist"]
check("getArtist albums sorted by year",
      [al["name"] for al in artist["album"]] == ["Waves", "Hits & Rarities <Live>"])
check("getArtist album attrs", artist["album"][1]["artistId"] == f"ar-{AURORA_ID}"
      and artist["album"][1]["songCount"] == 2 and artist["album"][1]["duration"] == 446)

r = get("/rest/getMusicDirectory", {"id": f"ar-{AURORA_ID}", "f": "json"})
directory = r.json()["subsonic-response"]["directory"]
check("getMusicDirectory(ar-) lists albums as dirs",
      {c["title"] for c in directory["child"]} == {"Waves", "Hits & Rarities <Live>"}
      and all(c["isDir"] for c in directory["child"]))

r = get("/rest/getMusicDirectory", {"id": f"al-{HITS_ID}", "f": "json"})
directory = r.json()["subsonic-response"]["directory"]
titles = {c["title"] for c in directory["child"]}
check("getMusicDirectory(al-) lists downloaded songs only",
      titles == {"Runaway", "Through the Eyes"}, str(titles))

r = get("/rest/getAlbum", {"id": f"al-{HITS_ID}", "f": "json"})
album = r.json()["subsonic-response"]["album"]
check("getAlbum name+song list", album["name"] == "Hits & Rarities <Live>"
      and [s["title"] for s in album["song"]] == ["Runaway", "Through the Eyes"])
song = album["song"][0]
check("song attrs (id/parent/artist/contentType/suffix)",
      song["id"] == f"tr-{T_RUNAWAY}" and song["parent"] == f"al-{HITS_ID}"
      and song["artist"] == "Aurora" and song["contentType"] == "audio/flac"
      and song["suffix"] == "flac" and song["genre"] == "Dream Pop")
check("album attrs (songCount/duration/year)",
      album["songCount"] == 2 and album["duration"] == 446 and album["year"] == 2021)

r = get("/rest/getAlbum", {"id": f"al-{HITS_ID}"})
xml_album = ET.fromstring(r.content).find("album")
check("getAlbum XML: song children + escaping",
      xml_album is not None and xml_album.get("name") == "Hits & Rarities <Live>"
      and len(xml_album.findall("song")) == 2)

r = get("/rest/getSong", {"id": f"tr-{T_RUNAWAY}", "f": "json"})
check("getSong roundtrip",
      r.json()["subsonic-response"]["song"]["title"] == "Runaway")
song = r.json()["subsonic-response"]["song"]
check("song carries coverArt pointing at its album",
      song["coverArt"] == f"al-{HITS_ID}" and song["type"] == "music"
      and song["isDir"] is False)

# ---------------------------------------------------------------------------
# 3. Lists / search / genres
# ---------------------------------------------------------------------------
print("lists + search")

r = get("/rest/getAlbumList2", {"type": "alphabeticalByName", "size": "2", "f": "json"})
al = r.json()["subsonic-response"]["albumList2"]["album"]
check("getAlbumList2 alphabeticalByName + size",
      [a["name"] for a in al] == ["Abbey Road", "Hits & Rarities <Live>"],
      str([a["name"] for a in al]))

r = get("/rest/getAlbumList2", {"type": "alphabeticalByName", "offset": "2", "f": "json"})
al = r.json()["subsonic-response"]["albumList2"]["album"]
check("getAlbumList2 offset", [a["name"] for a in al] == ["Waves"])

r = get("/rest/getAlbumList2", {"type": "newest", "f": "json"})
check("getAlbumList2 newest is a list", isinstance(
    r.json()["subsonic-response"]["albumList2"]["album"], list))

r = get("/rest/getAlbumList2", {"type": "byYear", "fromYear": "1960", "toYear": "2000",
                                "f": "json"})
al = r.json()["subsonic-response"]["albumList2"]["album"]
check("getAlbumList2 byYear range ascending", [a["name"] for a in al] == ["Abbey Road"])

r = get("/rest/getAlbumList2", {"type": "byYear", "fromYear": "2100", "toYear": "2000",
                                "f": "json"})
al = r.json()["subsonic-response"]["albumList2"]["album"]
check("getAlbumList2 byYear reversed range → descending",
      [a["name"] for a in al] == ["Hits & Rarities <Live>", "Waves"],
      str([a["name"] for a in al]))

r = get("/rest/getAlbumList2", {"type": "byGenre", "genre": "Dream Pop", "f": "json"})
al = r.json()["subsonic-response"]["albumList2"]["album"]
check("getAlbumList2 byGenre", [a["name"] for a in al] == ["Hits & Rarities <Live>"])

r = get("/rest/getAlbumList", {"type": "alphabeticalByName", "f": "json"})
al = r.json()["subsonic-response"]["albumList"]["album"]
check("getAlbumList (v1) directory-shaped", al[0].get("parent", "").startswith("ar-"))

r = get("/rest/search3", {"query": "beat", "f": "json"})
sr = r.json()["subsonic-response"]["searchResult3"]
check("search3 artist hit", any(a["name"] == "The Beatles" for a in sr["artist"]))

r = get("/rest/search3", {"query": "abbey", "f": "json"})
sr = r.json()["subsonic-response"]["searchResult3"]
check("search3 album hit", any(a["name"] == "Abbey Road" for a in sr["album"]))

r = get("/rest/search3", {"query": "here comes", "f": "json"})
sr = r.json()["subsonic-response"]["searchResult3"]
check("search3 song hit", any(s["title"] == "Here Comes the Sun" for s in sr["song"]))

r = get("/rest/search3", {"query": "Not Yet Downloaded", "f": "json"})
sr = r.json()["subsonic-response"]["searchResult3"]
check("search3 hides undownloaded songs (key omitted per JSON rules)",
      sr.get("song") is None, str(sr))

r = get("/rest/search3", {"query": "zzzznomatch", "f": "json"})
check("search3 zero results → present-but-empty wrapper",
      r.json()["subsonic-response"]["searchResult3"] == {})

r = get("/rest/search3", {"query": "", "f": "json"})
sr = r.json()["subsonic-response"]["searchResult3"]
check("search3 empty query lists the library",
      len(sr.get("song", [])) == 4 and len(sr.get("artist", [])) == 3,
      f"songs={len(sr.get('song', []))} artists={len(sr.get('artist', []))}")

r = get("/rest/search2", {"query": "runaway", "f": "json"})
check("search2 song hit",
      any(s["title"] == "Runaway"
          for s in r.json()["subsonic-response"]["searchResult2"]["song"]))

r = get("/rest/getGenres", {"f": "json"})
genres = {g["value"]: g for g in r.json()["subsonic-response"]["genres"]["genre"]}
check("getGenres counts", genres.get("Dream Pop", {}).get("songCount") == 2
      and genres.get("Dream Pop", {}).get("albumCount") == 1)

r = get("/rest/getGenres")
xml_genres = ET.fromstring(r.content).findall("genres/genre")
check("getGenres XML text content", any(g.text == "Dream Pop" for g in xml_genres))

r = get("/rest/getRandomSongs", {"size": "1", "f": "json"})
songs = r.json()["subsonic-response"]["randomSongs"]["song"]
check("getRandomSongs respects size", len(songs) == 1)

r = get("/rest/getStarred2", {"f": "json"})
check("getStarred2 empty ok", r.json()["subsonic-response"]["starred2"] == {})

r = get("/rest/getScanStatus", {"f": "json"})
check("getScanStatus counts all tracks",
      r.json()["subsonic-response"]["scanStatus"]["count"] == 5)

# ---------------------------------------------------------------------------
# 4. Media: stream / download / cover art
# ---------------------------------------------------------------------------
print("media delivery")

r = get("/rest/stream", {"id": f"tr-{T_RUNAWAY}"})
check("stream returns exact file bytes",
      r.content == audio_path.read_bytes() and r.status_code == 200)
check("stream content-type audio/flac", r.headers["Content-Type"] == "audio/flac")

r = get("/rest/download", {"id": f"tr-{T_RUNAWAY}", "f": "json"})
check("download alias works", r.status_code == 200)

r = get("/rest/stream", {"id": f"tr-{T_MISSING}", "f": "json"})
check("stream of undownloaded track → XML error 70 (binary endpoints)",
      r.status_code == 200 and b'code="70"' in r.content
      and r.headers["Content-Type"].startswith("text/xml"))

r = get("/rest/stream", {"id": f"tr-{T_ECHOES}", "f": "json"})
check("stream of row-without-file → XML error 70",
      r.status_code == 200 and b'code="70"' in r.content)

r = get("/rest/getCoverArt", {"id": f"al-{HITS_ID}"})
check("getCoverArt serves local cover.jpg",
      r.content == b"\xff\xd8\xff\xe0FAKEJPEG"
      and r.headers["Content-Type"] == "image/jpeg")

r = get("/rest/getCoverArt", {"id": f"ar-{AURORA_ID}", "f": "json"})
check("getCoverArt artist without image → XML error 70",
      r.status_code == 200 and b'code="70"' in r.content
      and r.headers["Content-Type"].startswith("text/xml"))

r = get("/rest/getCoverArt", {"id": "al-99999", "f": "json"})
check("getCoverArt unknown album → XML error 70",
      r.status_code == 200 and b'code="70"' in r.content)

r = get("/rest/getAlbum", {"id": "al-99999", "f": "json"})
check("getAlbum unknown album → error 70",
      r.json()["subsonic-response"]["error"]["code"] == 70)

r = get("/rest/getAlbum", {"f": "json"})
check("getAlbum missing id → error 10",
      r.json()["subsonic-response"]["error"]["code"] == 10)

r = get("/rest/getSimilarSongs2", {"f": "json"})
check("getSimilarSongs2 missing id → error 10",
      r.json()["subsonic-response"]["error"]["code"] == 10)

r = get("/rest/getCoverArt", {"f": "json"})
check("getCoverArt missing id → error 10",
      r.json()["subsonic-response"]["error"]["code"] == 10)

r = get("/rest/getSong", {"id": "tr-not-a-number", "f": "json"})
check("getSong malformed id → error 70",
      r.json()["subsonic-response"]["error"]["code"] == 70)

# ---------------------------------------------------------------------------
# 5. Similarity — AudioMuse DISABLED (local fallback) then ENABLED (mock)
# ---------------------------------------------------------------------------
print("similarity (AudioMuse off → local fallback)")

r = get("/rest/getSimilarSongs2", {"id": f"tr-{T_RUNAWAY}", "count": "10", "f": "json"})
similar = r.json()["subsonic-response"]["similarSongs2"]["similarSong"]
check("fallback = same-artist songs only",
      {s["artist"] for s in similar} == {"Aurora"}
      and all(s["id"] != f"tr-{T_RUNAWAY}" for s in similar), str(similar))

r = get("/rest/getSimilarSongs", {"id": f"tr-{T_RUNAWAY}", "f": "json"})
check("v1 getSimilarSongs also works",
      "similarSongs" in r.json()["subsonic-response"])

r = get("/rest/getArtistInfo2", {"id": f"ar-{AURORA_ID}", "count": "5", "f": "json"})
info = r.json()["subsonic-response"]["artistInfo2"]
names = {a["name"] for a in info["similarArtist"]}
check("fallback artistInfo2 = other library artists",
      names <= {"The Beatles", "Édith Piaf"} and len(names) == 2, str(info))

with app.app_context():
    set_setting("audiomuse_enabled", "true")
    set_setting("audiomuse_url", MOCK_URL)
    set_setting("audiomuse_api_token", MOCK_TOKEN)

print("similarity (AudioMuse on → mock instance)")

r = get("/rest/getSimilarSongs2", {"id": f"tr-{T_RUNAWAY}", "count": "10", "f": "json"})
similar = r.json()["subsonic-response"]["similarSongs2"]["similarSong"]
titles = [s["title"] for s in similar]
check("AudioMuse similarSongs: direct tr- id match lands first",
      titles[:1] == ["Echoes"], str(titles))
check("AudioMuse similarSongs: artist+title match mapped to fnack id",
      any(s["title"] == "Here Comes the Sun" and s["artistId"] == f"ar-{BEATLES_ID}"
          for s in similar))
check("AudioMuse unmatched entry dropped (still playable ids only)",
      "Unknown Song" not in titles)
check("AudioMuse request carried bearer token + expected params",
      MOCK_SEEN_AUTH[-1] == f"Bearer {MOCK_TOKEN}"
      and MOCK_SEEN_PARAMS.get("title") == "Runaway"
      and MOCK_SEEN_PARAMS.get("artist") == "Aurora"
      and MOCK_SEEN_PARAMS.get("eliminate_duplicates") == "true")

r = get("/rest/getArtistInfo2", {"id": f"ar-{AURORA_ID}", "count": "5", "f": "json"})
info = r.json()["subsonic-response"]["artistInfo2"]
names = [a["name"] for a in info["similarArtist"]]
check("AudioMuse similarArtist: stocked artist gets id",
      any(a["name"] == "The Beatles" and a.get("id") == f"ar-{BEATLES_ID}"
          for a in info["similarArtist"]), str(info))
check("AudioMuse similarArtist: unknown artist uses id='-1' (not-present convention)",
      any(a["name"] == "Ghost Artist" and a.get("id") == "-1"
          for a in info["similarArtist"]))

r = get("/rest/getArtistInfo", {"id": f"ar-{AURORA_ID}", "f": "json"})
check("v1 getArtistInfo key", "artistInfo" in r.json()["subsonic-response"])

with app.app_context():
    set_setting("audiomuse_api_token", "wrong-token")

r = get("/rest/getSimilarSongs2", {"id": f"tr-{T_RUNAWAY}", "f": "json"})
similar = r.json()["subsonic-response"]["similarSongs2"]["similarSong"]
check("AudioMuse 401 → graceful local fallback",
      {s["artist"] for s in similar} == {"Aurora"})

with app.app_context():
    set_setting("audiomuse_url", "http://127.0.0.1:9")  # closed port

r = get("/rest/getSimilarSongs2", {"id": f"tr-{T_RUNAWAY}", "f": "json"})
check("AudioMuse unreachable → graceful local fallback",
      r.json()["subsonic-response"]["status"] == "ok"
      and "similarSong" in r.json()["subsonic-response"]["similarSongs2"])

with app.app_context():
    set_setting("audiomuse_enabled", "false")
    set_setting("audiomuse_url", "")

r = get("/rest/getSimilarSongs2", {"id": f"tr-{T_RUNAWAY}", "f": "json"})
check("integration off again → plain fallback, no errors",
      r.json()["subsonic-response"]["status"] == "ok")

# ---------------------------------------------------------------------------
print()
if FAILED:
    print(f"SUBSONIC TEST FAILED — {len(FAILED)} failing: {FAILED}")
    sys.exit(1)
print("SUBSONIC TEST PASSED")
