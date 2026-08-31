"""Bundled first-party plugin: Subsonic API server extension.

Lets Subsonic clients (Symfonium, DSub, Sublime Music, ...) browse and
stream fnack's library directly — independent of Navidrome.

Auth: Subsonic clients send u/p (or t=token&s=salt). We authenticate against
fnack's M2M API key (context.library.get_api_key()). If no API key is set,
the API is open (matches fnack's zero-auth model). First-cut endpoints:
ping/getLicense/getArtists/getAlbumList2/getArtist/getAlbum/getSong/stream/
getCoverArt/getScanStatus. Raw file streaming (no transcoding — the
container has no ffmpeg).
"""

import hashlib
import os

from flask import Blueprint, Response, jsonify, request, send_file

from plugins.base import ServerExtensionPlugin

_MIME = {
    ".flac": "audio/flac", ".opus": "audio/ogg", ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav",
}


class SubsonicPlugin(ServerExtensionPlugin):
    # Brief 6 §2: settings via the standard schema modal (enabled checkbox);
    # the custom settings_tab card is retired. Note: the routes register
    # unconditionally today; the enabled flag is stored but not yet gating
    # route registration (pre-existing behavior, unchanged here).
    def _auth_ok(self, args: dict) -> bool:
        key = self.context.library.get_api_key()
        if not key:
            return True  # zero-auth model: no key configured = open
        p = args.get("p")
        t = args.get("t")
        s = args.get("s")
        if p and p == key:
            return True
        if t and s and hashlib.md5((key + s).encode()).hexdigest() == t:
            return True
        return False

    def _ok(self, payload: dict):
        return jsonify({"subsonic-response": {"status": "ok", "version": "1.16.1", **payload}})

    def _err(self, code: int, message: str):
        return jsonify({"subsonic-response": {"status": "failed", "version": "1.16.1",
                                              "error": {"code": code, "message": message}}}), 200

    def register_routes(self, blueprint: Blueprint) -> None:
        bp = blueprint

        @bp.route("/rest/ping", methods=["GET", "POST"])
        @bp.route("/rest/ping.view", methods=["GET", "POST"])
        @bp.route("/rest/getLicense", methods=["GET", "POST"])
        @bp.route("/rest/getLicense.view", methods=["GET", "POST"])
        def ping_license():
            if not self._auth_ok(request.values):
                return self._err(40, "Wrong username or password")
            return self._ok({"type": "fnack", "validUntil": "2035-01-01T00:00:00Z"})

        @bp.route("/rest/getArtists", methods=["GET", "POST"])
        @bp.route("/rest/getArtists.view", methods=["GET", "POST"])
        def get_artists():
            if not self._auth_ok(request.values):
                return self._err(40, "Wrong username or password")
            artists = self.context.library.list_artists()
            index = {}
            for a in artists:
                letter = (a["name"][0] or "#").upper()
                index.setdefault(letter, []).append(
                    {"id": f"ar-{a['id']}", "name": a["name"]})
            return self._ok({"artists": {"index": [
                {"name": k, "artist": v} for k, v in sorted(index.items())
            ]}})

        @bp.route("/rest/getAlbumList2", methods=["GET", "POST"])
        @bp.route("/rest/getAlbumList2.view", methods=["GET", "POST"])
        def get_album_list():
            if not self._auth_ok(request.values):
                return self._err(40, "Wrong username or password")
            albums = self.context.library.list_albums(limit=500)
            return self._ok({"albumList2": {"album": [
                {"id": f"al-{a['id']}", "name": a["name"], "year": a.get("year") or 0,
                 "artistId": f"ar-{a['artist_id']}", "songCount": 0}
                for a in albums
            ]}})

        @bp.route("/rest/getAlbum", methods=["GET", "POST"])
        @bp.route("/rest/getAlbum.view", methods=["GET", "POST"])
        def get_album():
            if not self._auth_ok(request.values):
                return self._err(40, "Wrong username or password")
            album_id = int((request.values.get("id") or "0").replace("al-", ""))
            album = self.context.library.get_album(album_id)
            if not album:
                return self._err(70, "Album not found")
            tracks = self.context.library.list_tracks(album_id=album_id)
            return self._ok({"album": {
                "id": f"al-{album['id']}", "name": album["name"], "year": album.get("year") or 0,
                "song": [{"id": f"tr-{t['id']}", "title": t["title"], "track": t["track_number"] or 0,
                          "duration": int(t.get("duration") or 0)} for t in tracks],
            }})

        @bp.route("/rest/getSong", methods=["GET", "POST"])
        @bp.route("/rest/getSong.view", methods=["GET", "POST"])
        def get_song():
            if not self._auth_ok(request.values):
                return self._err(40, "Wrong username or password")
            track_id = int((request.values.get("id") or "0").replace("tr-", ""))
            t = self.context.library.get_track(track_id)
            if not t:
                return self._err(70, "Song not found")
            return self._ok({"song": {"id": f"tr-{t['id']}", "title": t["title"],
                                      "duration": int(t.get("duration") or 0)}})

        @bp.route("/rest/stream", methods=["GET", "POST"])
        @bp.route("/rest/stream.view", methods=["GET", "POST"])
        def stream():
            if not self._auth_ok(request.values):
                return self._err(40, "Wrong username or password")
            track_id = int((request.values.get("id") or "0").replace("tr-", ""))
            t = self.context.library.get_track(track_id)
            if not t:
                return self._err(70, "Song not found")
            path = t.get("local_path") or t.get("file_path")
            if not path or not os.path.isfile(str(path)):
                return self._err(70, "File not found")
            ext = os.path.splitext(str(path))[1].lower()
            return send_file(str(path), mimetype=_MIME.get(ext, "application/octet-stream"))

        @bp.route("/rest/getCoverArt", methods=["GET", "POST"])
        @bp.route("/rest/getCoverArt.view", methods=["GET", "POST"])
        def cover():
            if not self._auth_ok(request.values):
                return self._err(40, "Wrong username or password")
            album_id = int((request.values.get("id") or "0").replace("al-", ""))
            album = self.context.library.get_album(album_id)
            return self._err(70, "Cover not found (not yet indexed)")

        @bp.route("/rest/getScanStatus", methods=["GET", "POST"])
        @bp.route("/rest/getScanStatus.view", methods=["GET", "POST"])
        @bp.route("/rest/startScan", methods=["GET", "POST"])
        @bp.route("/rest/startScan.view", methods=["GET", "POST"])
        def scan():
            if not self._auth_ok(request.values):
                return self._err(40, "Wrong username or password")
            return self._ok({"scanStatus": {"scanning": False, "count": 0}})
