"""Lidarr integration service: Full SABnzbd download client and Newznab/Torznab indexer emulation."""

import html
import logging
import re
import secrets
import time
from datetime import datetime
from email.utils import formatdate
from pathlib import Path
from flask import Flask, Response, jsonify, request

from models import AppSetting, Artist, DownloadJob, Track, db
from services.deezer_service import get_album_info, get_track_info, search_album, search_artist, search_track
from services.queue_service import queue_track

logger = logging.getLogger("fnack.lidarr")


def get_api_key(app: Flask) -> str:
    with app.app_context():
        setting = db.session.get(AppSetting, "api_key")
        if not setting or not setting.value:
            new_key = secrets.token_hex(16)
            if setting:
                setting.value = new_key
            else:
                db.session.add(AppSetting(key="api_key", value=new_key))
            db.session.commit()
            return new_key
        return setting.value


def verify_api_key(app: Flask) -> bool:
    key = get_api_key(app)
    provided = request.args.get("apikey") or request.headers.get("X-Api-Key", "") or request.values.get("apikey", "")
    return bool(key and provided == key)


def handle_sabnzbd_api(app: Flask):
    """Handle SABnzbd API emulation requests from Lidarr."""
    if not verify_api_key(app):
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
        item_type, item_id = _parse_grab()
        if item_type and item_id:
            job = _create_lidarr_grab_job(app, item_type, item_id)
            if job:
                return jsonify({"status": True, "nzo_ids": [f"SAB-{job.id}"]})
        return jsonify({"status": False, "error": "Could not parse item from NZB"}), 400

    if mode == "queue":
        slots = []
        with app.app_context():
            jobs = DownloadJob.query.filter(DownloadJob.status.in_(["queued", "downloading"])).all()
            for j in jobs:
                slots.append({
                    "nzo_id": f"SAB-{j.id}",
                    "filename": j.album_name,
                    "mb": 0,
                    "mbleft": 0,
                    "percentage": str(int(j.progress)),
                    "status": "Downloading" if j.status == "downloading" else "Queued",
                })
        return jsonify({"queue": {"slots": slots, "status": "Downloading"}})

    if mode == "history":
        slots = []
        with app.app_context():
            jobs = DownloadJob.query.filter(DownloadJob.status.in_(["completed", "failed", "error"])).all()
            for j in jobs:
                artist_name = j.artist.name if j.artist else "Unknown"
                storage_path = f"/downloads/{artist_name}/{j.album_name}" if j.source == "lidarr" else f"/music/{artist_name}/{j.album_name}"
                slots.append({
                    "nzo_id": f"SAB-{j.id}",
                    "name": j.album_name,
                    "status": "Completed" if j.status == "completed" else "Failed",
                    "storage": storage_path,
                })
        return jsonify({"history": {"slots": slots}})

    if mode == "delete":
        nzo = request.values.get("value", "")
        jid = nzo.replace("SAB-", "")
        if jid.isdigit():
            with app.app_context():
                j = db.session.get(DownloadJob, int(jid))
                if j:
                    j.status = "cancelled"
                    db.session.commit()
        return jsonify({"status": True})

    return jsonify({"error": "Unknown mode"}), 400


def handle_newznab_api(app: Flask):
    """Handle Newznab / Torznab RSS & search emulation requests from Lidarr."""
    t = request.args.get("t", "")

    if t == "caps":
        return _caps_xml()

    if not verify_api_key(app):
        return jsonify({"error": "Invalid API key"}), 401

    if t == "get":
        return _get_nzb(app)

    return _search_newznab(app)


def _parse_grab():
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
        except Exception:
            pass

    name = request.values.get("name") or ""
    m = re.search(r"/api/nzb/(album|track)/(\d+)", name)
    if m:
        return m.group(1), int(m.group(2))

    return None, None


def _create_lidarr_grab_job(app: Flask, item_type: str, item_id: int):
    """Create a download job for Lidarr grab."""
    with app.app_context():
        if item_type == "track":
            info = get_track_info(item_id)
            artist_name = info.get("artist_name") or "Unknown Artist"
            title = info.get("title") or "Unknown Track"
        else:
            info = get_album_info(item_id)
            artist_name = info.get("artist_name") or "Unknown Artist"
            title = info.get("title") or "Unknown Album"

        artist = Artist.query.filter_by(name=artist_name).first()
        if not artist:
            artist = Artist(spotify_id=f"lidarr:{artist_name}", name=artist_name, source="lidarr")
            db.session.add(artist)
            db.session.flush()

        job = DownloadJob(
            artist_id=artist.id,
            item_type=item_type,
            album_spotify_id=str(item_id),
            album_name=title,
            album_type=item_type,
            cover_url=info.get("cover_url"),
            status="queued",
            source="lidarr",
        )
        db.session.add(job)
        db.session.commit()
        logger.info("[LIDARR] Created grab job %d for %s '%s'", job.id, item_type, title)
        return job


def _caps_xml():
    xml = (
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
    return Response(xml, mimetype="application/xml")


def _get_nzb(app: Flask):
    job_id = request.args.get("id", type=int)
    title = f"Release {job_id}"
    item_type = "album"
    item_id = job_id or 0

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


def _search_newznab(app: Flask):
    q = request.args.get("q", "").strip()
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()

    items = []
    if artist or album:
        query_str = f"{artist} {album}".strip()
        for a in search_album(query_str, limit=10):
            items.append((a["artist_name"], a["title"], "album", a["id"], a.get("year")))
    elif q:
        for a in search_album(q, limit=10):
            items.append((a["artist_name"], a["title"], "album", a["id"], a.get("year")))
        for t in search_track(q, limit=10):
            items.append((t["artist_name"], t["title"], "track", t["id"], None))

    if not items:
        # Dummy result for indexer test connection
        items = [("fnack", "Connection Test", "album", 0, None)]

    base_url = request.host_url.rstrip("/")
    api_key = get_api_key(app)

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
