"""Bundled first-party plugin: Lidarr integration (Brief 7 §1b/§4).

Lets Lidarr use fnack as its indexer (Newznab/Torznab) AND its download
client (SABnzbd). Grabs arrive over the emulated APIs and are expanded into
fnack's own library (Artist / Album / Track rows + queued DownloadJobs), so
downloads flow through the normal queue pipeline exactly like any other
track.

This is the former `services/lidarr_service.py`, extracted per the core vs.
plugin decision rule: the emulation is optional, swappable, and only useful
to people who run Lidarr — it does not belong in core. All DB access goes
through `context.library` (queue_lidarr_grab / list_download_jobs /
cancel_download_job / search_* / get_*_info); this module only owns the
HTTP protocol layer (XML/JSON building, auth, request parsing).

Auth: fnack's M2M API key (context.library.get_api_key()). Zero-auth model:
if no key is set, the API is open — matches fnack's standing constraint.
"""

import html
import re
import time
from datetime import datetime
from email.utils import formatdate
from typing import Optional

from flask import Blueprint, Response, jsonify, request

from plugins.base import LibrarySourcePlugin, ServerExtensionPlugin

_CAPS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<caps>\n'
    '  <server title="fnack" />\n'
    '  <searching>\n'
    '    <search available="yes" supportedParams="q" />\n'
    '    <music-search available="yes" supportedParams="q,artist,album,year" />\n'
    '  </searching>\n'
    '  <categories>\n'
    '    <category id="3000" name="Audio">\n'
    '      <subcat id="3010" name="MP3" />\n'
    '      <subcat id="3020" name="FLAC" />\n'
    '      <subcat id="3040" name="Lossless" />\n'
    '    </category>\n'
    '  </categories>\n'
    '</caps>'
)


class LidarrPlugin(LibrarySourcePlugin, ServerExtensionPlugin):
    # -- LibrarySourcePlugin interface --------------------------------------

    def list_artists(self) -> list[dict]:
        """Lidarr pushes grabs into fnack (via the emulated APIs); fnack does
        not pull an artist list back from Lidarr, so there is nothing to
        enumerate. The 'source' here is the grab flow, not a poll."""
        return []

    # -- helpers -------------------------------------------------------------

    def _verify_api_key(self) -> bool:
        key = self.context.library.get_or_create_api_key()
        provided = (request.args.get("apikey")
                    or request.headers.get("X-Api-Key", "")
                    or request.values.get("apikey", ""))
        return bool(key and provided == key)

    def _handle_sabnzbd(self):
        """SABnzbd download-client emulation (mode-dispatch)."""
        if not self._verify_api_key():
            return jsonify({"error": "Invalid API key"}), 401

        mode = request.values.get("mode", "")

        if mode in ("version", "get_config"):
            return jsonify({
                "version": "3.8.0",
                "config": {
                    "misc": {"complete_dir": "/downloads"},
                    "categories": [
                        {"name": "*", "dir": ""},
                        {"name": "music", "dir": "music"},
                        {"name": "default", "dir": ""},
                    ],
                },
            })

        if mode in ("addurl", "addfile"):
            item_type, item_id = self._parse_grab()
            if item_type and item_id:
                job_ids = self.context.library.queue_lidarr_grab(item_type, item_id)
                if job_ids:
                    return jsonify({"status": True, "nzo_ids": [f"SAB-{jid}" for jid in job_ids]})
            return jsonify({"status": False, "error": "Could not parse item from NZB"}), 400

        if mode == "queue":
            jobs = self.context.library.list_download_jobs(["queued", "downloading"])
            slots = [{
                "nzo_id": f"SAB-{j['id']}",
                "filename": j["album_name"],
                "mb": 0,
                "mbleft": 0,
                "percentage": str(int(j["progress"])),
                "status": "Downloading" if j["status"] == "downloading" else "Queued",
            } for j in jobs]
            return jsonify({"queue": {"slots": slots, "status": "Downloading"}})

        if mode == "history":
            jobs = self.context.library.list_download_jobs(["completed", "failed", "error"])
            slots = []
            for j in jobs:
                storage_path = (f"/downloads/{j['artist_name']}/{j['album_name']}"
                                if j["source"] == "lidarr"
                                else f"/music/{j['artist_name']}/{j['album_name']}")
                slots.append({
                    "nzo_id": f"SAB-{j['id']}",
                    "name": j["album_name"],
                    "status": "Completed" if j["status"] == "completed" else "Failed",
                    "storage": storage_path,
                })
            return jsonify({"history": {"slots": slots}})

        if mode == "delete":
            nzo = request.values.get("value", "")
            jid = nzo.replace("SAB-", "")
            if jid.isdigit():
                self.context.library.cancel_download_job(int(jid))
            return jsonify({"status": True})

        return jsonify({"error": "Unknown mode"}), 400

    def _handle_newznab(self):
        """Newznab / Torznab indexer emulation (t= caps|get|search)."""
        t = request.args.get("t", "")

        if t == "caps":
            return Response(_CAPS_XML, mimetype="application/xml")

        if not self._verify_api_key():
            return jsonify({"error": "Invalid API key"}), 401

        if t == "get":
            return self._get_nzb()

        return self._search_newznab()

    def _parse_grab(self):
        nzb_file = request.files.get("nzbfile") or request.files.get("file")
        if nzb_file:
            try:
                body = nzb_file.read().decode("utf-8", "ignore")
                m = re.search(r"<item_type>\s*([^<\s]+)\s*</item_type>", body)
                itype = m.group(1).strip() if m else None
                m = re.search(r"<item_id>\s*(\d+)\s*</item_id>", body)
                iid = int(m.group(1)) if m else None
                if itype and iid:
                    return itype, iid
            except Exception:  # noqa: BLE001 - protocol parsing, fail soft
                pass

        name = request.values.get("name") or ""
        m = re.search(r"/api/nzb/(album|track)/(\d+)", name)
        if m:
            return m.group(1), int(m.group(2))

        return None, None

    def _get_nzb(self, item_type: Optional[str] = None, item_id: Optional[int] = None):
        """Build the NZB file Lidarr sends to its download client. The NZB
        body embeds <item_type>/<item_id> so the SABnzbd emulation can parse
        the grab back out when Lidarr POSTs it."""
        if not item_id:
            item_id = request.args.get("id", type=int) or 0
        item_type = item_type or "album"

        # Nicer release names when the item is known to Deezer
        title = f"Release {item_id}"
        try:
            if item_type == "track":
                info = self.context.library.get_track_info(item_id)
                title = f"{info.get('artist_name', '')} - {info.get('title', '')}"
            elif item_id:
                info = self.context.library.get_album_info(item_id)
                title = f"{info.get('artist_name', '')} - {info.get('title', '')}"
        except Exception:  # noqa: BLE001 - title is cosmetic, fail soft
            pass

        nzb = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">\n'
            '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">\n'
            '  <head>\n'
            '    <meta type="fnack">\n'
            f'      <item_type>{item_type}</item_type>\n'
            f'      <item_id>{item_id}</item_id>\n'
            '    </meta>\n'
            f'    <meta type="title">{html.escape(title)}</meta>\n'
            '  </head>\n'
            f'  <file poster="fnack" date="{int(time.time())}" subject="{html.escape(title)}">\n'
            '    <groups><group>alt.binaries.sounds</group></groups>\n'
            '    <segments><segment bytes="1024" number="1">fnack-dummy</segment></segments>\n'
            '  </file>\n'
            '</nzb>'
        )
        resp = Response(nzb, mimetype="application/x-nzb")
        resp.headers["Content-Disposition"] = f'attachment; filename="fnack-{item_id}.nzb"'
        return resp

    def _search_newznab(self):
        q = request.args.get("q", "").strip()
        artist = request.args.get("artist", "").strip()
        album = request.args.get("album", "").strip()

        items = []
        if artist or album:
            query_str = f"{artist} {album}".strip()
            for a in self.context.library.search_albums(query_str, limit=10):
                items.append((a["artist_name"], a["title"], "album", a["id"], a.get("year")))
        elif q:
            for a in self.context.library.search_albums(q, limit=10):
                items.append((a["artist_name"], a["title"], "album", a["id"], a.get("year")))
            for t in self.context.library.search_tracks(q, limit=10):
                items.append((t["artist_name"], t["title"], "track", t["id"], None))

        if not items:
            # Dummy result for indexer test connection
            items = [("fnack", "Connection Test", "album", 0, None)]

        base_url = request.host_url.rstrip("/")
        api_key = self.context.library.get_or_create_api_key()

        items_xml = []
        for art, alb, itype, deezer_id, year in items:
            title_str = f"{art} - {alb}" + (f" ({year})" if year else "") + " [FLAC]"
            link_url = f"{base_url}/api/nzb/{itype}/{deezer_id}?apikey={api_key}"
            size_bytes = 300 * 1024 * 1024 if itype == "album" else 30 * 1024 * 1024
            items_xml.append(
                f'    <item>\n'
                f'      <title>{html.escape(title_str)}</title>\n'
                f'      <guid isPermaLink="false">fnack-{itype}-{deezer_id}</guid>\n'
                f'      <category>3000</category>\n'
                f'      <size>{size_bytes}</size>\n'
                f'      <pubDate>{datetime.now().strftime("%Y-%m-%d")}</pubDate>\n'
                f'      <link>{html.escape(link_url)}</link>\n'
                f'      <enclosure url="{html.escape(link_url)}" length="{size_bytes}" type="audio/mpeg" />\n'
                f'      <newznab:attr name="category" value="3000"/>\n'
                f'      <newznab:attr name="category" value="3020"/>\n'
                f'      <newznab:attr name="size" value="{size_bytes}"/>\n'
                f'      <newznab:attr name="artist" value="{html.escape(art)}"/>\n'
                f'      <newznab:attr name="album" value="{html.escape(alb)}"/>\n'
                f'    </item>\n'
            )

        feed = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">\n'
            '  <channel>\n'
            '    <title>fnack</title>\n'
            f'    <pubDate>{formatdate(usegmt=True)}</pubDate>\n'
            + "".join(items_xml)
            + '  </channel>\n</rss>'
        )
        return Response(feed, mimetype="application/xml")

    # -- settings ------------------------------------------------------------

    def on_load(self) -> None:
        # Seed the plugin's api_key field from the core M2M key so the
        # settings modal shows what Lidarr must use. The core key stays the
        # single source of truth; this only mirrors it for display. Matches
        # the former lidarr_service.get_api_key(app) read-or-generate.
        if not self.context.settings.get("api_key"):
            self.context.settings.set("api_key", self.context.library.get_or_create_api_key())

    def on_settings_changed(self, settings: dict) -> None:
        # Write a non-empty api_key through to the core M2M key (Lidarr and
        # fnack's other M2M surfaces share the same key). Empty = leave
        # untouched (never wipe the key by saving a blank field).
        val = str(settings.get("api_key") or "").strip()
        if val:
            self.context.library.set_setting("api_key", val)

    # -- ServerExtensionPlugin interface -------------------------------------

    def register_routes(self, blueprint: Blueprint) -> None:
        bp = blueprint

        @bp.route("/api/sabnzbd", methods=["GET", "POST"])
        @bp.route("/api/sabnzbd/api", methods=["GET", "POST"])
        @bp.route("/sabnzbd/api", methods=["GET", "POST"])
        def sabnzbd_proxy():
            return self._handle_sabnzbd()

        @bp.route("/api/newznab", methods=["GET"])
        @bp.route("/api/newznab/api", methods=["GET"])
        @bp.route("/api/torznab", methods=["GET"])
        @bp.route("/api/torznab/api", methods=["GET"])
        @bp.route("/torznab/api", methods=["GET"])
        def newznab_proxy():
            return self._handle_newznab()

        @bp.route("/api/nzb/<item_type>/<int:item_id>", methods=["GET"])
        def api_nzb_grab(item_type, item_id):
            # Lidarr downloads the <link> from the search feed verbatim (no
            # ?t=get), so serve the real NZB directly from the path.
            return self._get_nzb(item_type=item_type, item_id=item_id)
