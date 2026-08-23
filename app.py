#!/usr/bin/env python3
"""fnack – Modern Lossless Music Discography Downloader & Manager.

A modular Flask + SocketIO application with Deezer metadata ingestion,
ISRC-first SpotiFLAC / yt-dlp download pipeline, Sonarr-style artist discography views,
interactive library import, and full Lidarr emulation.
"""

import nest_asyncio
nest_asyncio.apply()

from dotenv import load_dotenv
load_dotenv()

from gevent import monkey
monkey.patch_all()

import logging
import os
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import gevent
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

from models import Album, AppSetting, Artist, DownloadJob, Track, db
from services.deezer_service import (
    get_artist_discography,
    get_artist_info,
    search_artist,
)
from services.import_service import import_artist_folder, scan_root_folder_candidates
from services.lidarr_service import (
    get_api_key,
    handle_newznab_api,
    handle_sabnzbd_api,
)
from services.navidrome_service import test_navidrome_connection, trigger_navidrome_scan
from services.watcher_service import start_folder_watcher
from services.queue_service import (
    cancel_job,
    download_manual_match_track,
    queue_album,
    queue_artist_missing,
    queue_track,
    start_queue_worker,
)
from services.ytdlp_service import get_cookies_path, get_cookies_status

from sqlalchemy import case, event, func
from sqlalchemy.engine import Engine
from version import __version__

# Application initialization
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.urandom(24).hex()

@app.context_processor
def inject_app_context():
    return {"app_version": __version__}

_db_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
if not _db_dir.exists() and not os.environ.get("SQLALCHEMY_DATABASE_URI"):
    _fallback_config = Path(__file__).resolve().parent / "config"
    _fallback_config.mkdir(parents=True, exist_ok=True)
    _db_path = str(_fallback_config / "fnack.db")
else:
    _db_path = str(_db_dir / "fnack.db")

app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("SQLALCHEMY_DATABASE_URI", f"sqlite:///{_db_path}")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONCURRENT_DEFAULT"] = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3"))

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Enable Write-Ahead Logging (WAL) and memory optimizations for non-blocking concurrent reads/writes."""
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=-64000")  # 64MB cache
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA mmap_size=268435456")  # 256MB mmap
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA wal_autocheckpoint=1000")  # Auto-checkpoint every 1000 pages (~4MB)
        cursor.close()
    except Exception:
        pass

db.init_app(app)
socketio = SocketIO(app, async_mode="gevent", cors_allowed_origins="*")

# Structured logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fnack")


class _AccessLogFilter(logging.Filter):
    """Suppress noisy polling/socketio requests from WSGI access log."""
    def filter(self, record):
        msg = record.getMessage()
        if "GET /socket.io/" in msg or "POST /socket.io/" in msg:
            return False
        if "/static/" in msg:
            return False
        return True


for _name in ("werkzeug", "geventwebsocket.handler", "gunicorn.access", "gunicorn.error"):
    logging.getLogger(_name).addFilter(_AccessLogFilter())

for _noisy in ("primp", "ddgs", "ddgs.ddgs", "urllib3", "curl_cffi", "duckduckgo_search"):
    _nl = logging.getLogger(_noisy)
    _nl.setLevel(logging.CRITICAL)
    _nl.propagate = False
    _nl.disabled = True


def _get_setting(key: str, default: str = "") -> str:
    s = db.session.get(AppSetting, key)
    return s.value if s else default


def _set_setting(key: str, value: str) -> None:
    s = db.session.get(AppSetting, key)
    if s:
        s.value = value
    else:
        db.session.add(AppSetting(key=key, value=value))
    db.session.commit()


# ══════════════════════════════════════════════════════════════════════
#  Frontend Page Routes
# ══════════════════════════════════════════════════════════════════════

@app.route("/")
def page_index():
    return render_template("index.html")


@app.route("/artist/<int:artist_id>")
def page_artist(artist_id):
    return render_template("artist.html", artist_id=artist_id)


@app.route("/import")
def page_import():
    return render_template("import.html")


@app.route("/queue")
def page_queue():
    return render_template("queue.html")


@app.route("/settings")
def page_settings():
    return render_template("settings.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


# ══════════════════════════════════════════════════════════════════════
#  Search & Artist Onboarding API
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/search-artist")
def api_search_artist():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify([])
    try:
        results = search_artist(q, limit=8)
        return jsonify(results)
    except Exception as e:
        logger.exception("Artist search failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artists")
def api_artists():
    # Ultra-fast indexed aggregation queries (no Cartesian outer join with distinct hash overhead)
    album_counts = dict(
        db.session.query(Album.artist_id, func.count(Album.id))
        .group_by(Album.artist_id)
        .all()
    )

    track_stats = dict(
        (row[0], {"total_tracks": row[1] or 0, "downloaded_tracks": int(row[2] or 0)})
        for row in db.session.query(
            Album.artist_id,
            func.count(Track.id),
            func.sum(case((Track.is_downloaded == True, 1), else_=0)),
        )
        .join(Track, Track.album_id == Album.id)
        .group_by(Album.artist_id)
        .all()
    )

    artists = Artist.query.order_by(Artist.name).all()
    out = []
    for a in artists:
        st = track_stats.get(a.id, {"total_tracks": 0, "downloaded_tracks": 0})
        tot_t = st["total_tracks"]
        dl_t = st["downloaded_tracks"]
        tot_alb = album_counts.get(a.id, 0)
        out.append({
            "id": a.id,
            "name": a.name,
            "image_url": a.image_url,
            "monitored": bool(a.monitored),
            "auto_download": bool(a.auto_download),
            "source": a.source,
            "sync_status": a.sync_status,
            "sync_error": a.sync_error,
            "last_synced_at": a.last_synced_at.isoformat() if a.last_synced_at else None,
            "total_albums": tot_alb,
            "total_tracks": tot_t,
            "downloaded_tracks": dl_t,
            "percent_downloaded": round((dl_t / tot_t * 100), 1) if tot_t else 0,
        })
    return jsonify(out)


@app.route("/api/stats")
def api_stats():
    """Global catalogue statistics (total artists, downloaded tracks, failed songs, catalogue size in GB/TB)."""
    total_artists = Artist.query.count()
    monitored_artists = Artist.query.filter_by(monitored=True).count()

    res = db.session.query(
        func.count(Track.id),
        func.sum(case((Track.is_downloaded == True, 1), else_=0)),
        func.sum(case(((Track.status.in_(["failed", "error"])) | ((Track.is_downloaded == False) & (Track.error_message.isnot(None)) & (Track.error_message != "")), 1), else_=0)),
        func.sum(case((Track.is_downloaded == True, Track.size_bytes), else_=0)),
    ).first()

    total_tracks = res[0] or 0
    downloaded_tracks = int(res[1] or 0)
    failed_tracks = int(res[2] or 0)
    total_size_bytes = int(res[3] or 0)
    missing_tracks = max(0, total_tracks - downloaded_tracks - failed_tracks)

    if total_size_bytes >= 1024 * 1024 * 1024 * 1024:
        size_formatted = f"{total_size_bytes / (1024 ** 4):.2f} TB"
    elif total_size_bytes >= 1024 * 1024 * 1024:
        size_formatted = f"{total_size_bytes / (1024 ** 3):.2f} GB"
    elif total_size_bytes >= 1024 * 1024:
        size_formatted = f"{total_size_bytes / (1024 ** 2):.1f} MB"
    else:
        size_formatted = f"{total_size_bytes // 1024} KB" if total_size_bytes else "0 MB"

    return jsonify({
        "total_artists": total_artists,
        "monitored_artists": monitored_artists,
        "total_tracks": total_tracks,
        "downloaded_tracks": downloaded_tracks,
        "failed_tracks": failed_tracks,
        "missing_tracks": missing_tracks,
        "total_size_bytes": total_size_bytes,
        "total_size_formatted": size_formatted,
    })


def _sync_artist_discography_background(artist_id: int, deezer_artist_id: int, options: dict):
    """Background task to fetch artist discography from Deezer and index albums & tracks."""
    with app.app_context():
        artist = db.session.get(Artist, artist_id)
        if not artist:
            return

        artist.sync_status = "syncing"
        artist.sync_error = None
        db.session.commit()

        socketio.emit("artist_updated", {"artist_id": artist_id, "sync_status": "syncing"})
        logger.info("[DEEZER] Starting background discography fetch for artist '%s' (%d)", artist.name, deezer_artist_id)

    try:
        disco = get_artist_discography(
            deezer_artist_id,
            filter_remixes=options.get("filter_remixes", True),
            filter_lofi=options.get("filter_lofi", True),
            filter_live=options.get("filter_live", True),
            filter_compilations=options.get("filter_compilations", True),
            include_albums=options.get("include_albums", True),
            include_singles=options.get("include_singles", True),
            include_compilations=options.get("include_compilations", False),
        )

        with app.app_context():
            artist = db.session.get(Artist, artist_id)
            if not artist:
                return

            if disco.get("artist_image") and not artist.image_url:
                artist.image_url = disco["artist_image"]

            # Save albums and tracks
            for a in disco.get("albums", []):
                album = Album.query.filter_by(artist_id=artist.id, deezer_id=str(a["id"])).first()
                if not album:
                    album = Album(
                        artist_id=artist.id,
                        name=a["title"],
                        year=a.get("year"),
                        cover_url=a.get("cover_url"),
                        deezer_id=str(a["id"]),
                        record_type=a.get("record_type", "album"),
                    )
                    db.session.add(album)
                    db.session.flush()
                else:
                    if a.get("cover_url") and not album.cover_url:
                        album.cover_url = a["cover_url"]
                    if a.get("year") and not album.year:
                        album.year = a["year"]
                    if a.get("record_type") and not album.record_type:
                        album.record_type = a["record_type"]

                for t in a.get("tracks", []):
                    track = Track.query.filter_by(album_id=album.id, deezer_id=str(t["id"])).first()
                    if not track:
                        track = Track(
                            album_id=album.id,
                            artist_id=artist.id,
                            title=t["title"],
                            track_number=t.get("track_position"),
                            disc_number=t.get("disk_number", 1),
                            duration=t.get("duration"),
                            isrc=t.get("isrc"),
                            deezer_id=str(t["id"]),
                            status="missing",
                        )
                        db.session.add(track)
                        db.session.flush()
                    else:
                        if t.get("isrc") and not track.isrc:
                            track.isrc = t["isrc"]
                        if t.get("duration") and not track.duration:
                            track.duration = t["duration"]

            valid_deezer_ids = {str(a["id"]) for a in disco.get("albums", [])}

            # Prune stale or misattributed albums that are not downloaded and no longer in discography
            for existing_alb in artist.albums.all():
                if existing_alb.deezer_id not in valid_deezer_ids and not existing_alb.is_downloaded:
                    has_downloaded = any(t.is_downloaded for t in existing_alb.tracks.all())
                    if not has_downloaded:
                        logger.info("[DEEZER] Pruning stale/misattributed album '%s' (id=%d) for artist '%s'", existing_alb.name, existing_alb.id, artist.name)
                        for t in existing_alb.tracks.all():
                            DownloadJob.query.filter_by(track_id=t.id).delete()
                            db.session.delete(t)
                        db.session.delete(existing_alb)
            db.session.flush()

            artist.sync_status = "ready"
            artist.sync_error = None
            artist.last_synced_at = datetime.now(timezone.utc)
            db.session.commit()

            logger.info("[DEEZER] Discography sync complete for '%s' (%d albums)", artist.name, len(disco.get("albums", [])))

            # If auto_download enabled, queue missing tracks
            if artist.auto_download:
                queued = queue_artist_missing(app, artist.id, source="auto")
                logger.info("[DEEZER] Auto-download queued %d tracks for '%s'", queued, artist.name)

        socketio.emit("artist_synced", {"artist_id": artist_id, "sync_status": "ready"})
        socketio.emit("toast", {"message": f"Discography indexed for '{disco['artist_name']}'", "type": "success"})

    except Exception as e:
        logger.exception("[DEEZER] Discography ingestion failed for artist %d: %s", artist_id, e)
        with app.app_context():
            artist = db.session.get(Artist, artist_id)
            if artist:
                artist.sync_status = "error"
                artist.sync_error = str(e)
                db.session.commit()
        socketio.emit("artist_updated", {"artist_id": artist_id, "sync_status": "error", "error": str(e)})


@app.route("/api/add-artist", methods=["POST"])
def api_add_artist():
    data = request.get_json(silent=True) or {}
    deezer_id = data.get("id")
    if not deezer_id:
        return jsonify({"error": "No artist ID provided"}), 400

    existing = Artist.query.filter_by(spotify_id=str(deezer_id)).first()
    if existing:
        return jsonify({"message": f"Artist '{existing.name}' is already in your library.", "artist_id": existing.id}), 200

    try:
        artist_info = get_artist_info(int(deezer_id))
    except Exception as e:
        return jsonify({"error": f"Failed to reach Deezer: {e}"}), 500

    # Save artist record immediately so UI shows placeholder instantly
    artist = Artist(
        spotify_id=str(deezer_id),
        name=artist_info["name"],
        image_url=artist_info.get("image_url"),
        monitored=bool(data.get("monitored", True)),
        auto_download=bool(data.get("auto_download", False)),
        filter_remixes=bool(data.get("filter_remixes", True)),
        filter_lofi=bool(data.get("filter_lofi", True)),
        filter_live=bool(data.get("filter_live", True)),
        filter_compilations=bool(data.get("filter_compilations", True)),
        include_albums=bool(data.get("include_albums", True)),
        include_singles=bool(data.get("include_singles", True)),
        include_compilations=bool(data.get("include_compilations", False)),
        sync_status="syncing",
    )
    db.session.add(artist)
    db.session.commit()

    socketio.emit("artist_added", {"artist_id": artist.id, "artist_name": artist.name, "image_url": artist.image_url})

    # Start background ingestion
    socketio.start_background_task(
        _sync_artist_discography_background, artist.id, int(deezer_id), data
    )

    return jsonify({
        "message": f"Artist '{artist.name}' added. Fetching discography in background.",
        "artist_id": artist.id,
        "artist_name": artist.name,
    }), 202


# ══════════════════════════════════════════════════════════════════════
#  Dedicated Artist Details & Actions API
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/artist/<int:artist_id>")
def api_get_artist(artist_id):
    artist = db.session.get(Artist, artist_id)
    if not artist:
        return jsonify({"error": "Artist not found"}), 404

    # Group albums by record_type
    albums_data = []
    for album in artist.albums.order_by(Album.year.desc().nullslast(), Album.name).all():
        tracks_data = []
        for t in album.tracks.order_by(Track.disc_number, Track.track_number, Track.title).all():
            tracks_data.append({
                "id": t.id,
                "title": t.title,
                "track_number": t.track_number,
                "disc_number": t.disc_number,
                "duration": t.duration,
                "isrc": t.isrc,
                "status": t.status,
                "monitored": bool(getattr(t, "monitored", True)),
                "progress": t.progress,
                "is_downloaded": t.is_downloaded,
                "local_path": t.local_path,
                "file_path": t.file_path,
                "file_format": t.file_format,
                "bitrate": t.bitrate,
                "size_bytes": t.size_bytes,
                "error_message": t.error_message,
                "is_unmatched": t.is_unmatched,
            })

        albums_data.append({
            "id": album.id,
            "name": album.name,
            "year": album.year,
            "cover_url": album.cover_url,
            "record_type": album.record_type,
            "is_downloaded": album.is_downloaded,
            "monitored": bool(getattr(album, "monitored", True)),
            "size_bytes": album.size_bytes,
            "local_path": album.local_path,
            "track_count": len(tracks_data),
            "downloaded_count": sum(1 for t in tracks_data if t["is_downloaded"]),
            "tracks": tracks_data,
        })

    all_tracks = [t for a in albums_data for t in a["tracks"]]
    total_tracks = len(all_tracks)
    downloaded_tracks = sum(1 for t in all_tracks if t["is_downloaded"])

    return jsonify({
        "id": artist.id,
        "name": artist.name,
        "image_url": artist.image_url,
        "monitored": bool(artist.monitored),
        "auto_download": bool(artist.auto_download),
        "source": artist.source,
        "sync_status": artist.sync_status,
        "sync_error": artist.sync_error,
        "last_synced_at": artist.last_synced_at.isoformat() if artist.last_synced_at else None,
        "filter_remixes": bool(artist.filter_remixes),
        "filter_lofi": bool(artist.filter_lofi),
        "filter_live": bool(artist.filter_live),
        "filter_compilations": bool(artist.filter_compilations),
        "include_albums": bool(artist.include_albums),
        "include_singles": bool(artist.include_singles),
        "include_compilations": bool(artist.include_compilations),
        "total_albums": len(albums_data),
        "total_tracks": total_tracks,
        "downloaded_tracks": downloaded_tracks,
        "percent_downloaded": round((downloaded_tracks / total_tracks * 100), 1) if total_tracks else 0,
        "total_size_bytes": sum(a["size_bytes"] for a in albums_data),
        "albums": albums_data,
    })


@app.route("/api/artist/<int:artist_id>", methods=["DELETE"])
def api_delete_artist(artist_id):
    artist = db.session.get(Artist, artist_id)
    if not artist:
        return jsonify({"error": "Artist not found"}), 404

    name = artist.name
    del_files = request.args.get("delete_files", "false").lower() == "true"

    db.session.delete(artist)
    db.session.commit()

    deleted_disk = False
    if del_files:
        music_path = Path(_get_setting("music_path", "/music"))
        for folder in music_path.iterdir():
            if folder.is_dir() and folder.name.lower() == name.lower():
                try:
                    shutil.rmtree(str(folder))
                    deleted_disk = True
                except OSError:
                    pass

    socketio.emit("artist_deleted", {"artist_id": artist_id})
    return jsonify({"message": f"Artist '{name}' removed.", "files_deleted": deleted_disk})


@app.route("/api/album/<int:album_id>", methods=["DELETE"])
def api_delete_album(album_id):
    album = db.session.get(Album, album_id)
    if not album:
        return jsonify({"error": "Album not found"}), 404

    artist_id = album.artist_id
    album_name = album.name
    del_files = request.args.get("delete_files", "false").lower() == "true"

    if del_files and album.local_path:
        p = Path(album.local_path)
        if p.exists() and p.is_dir():
            try:
                shutil.rmtree(str(p))
            except OSError:
                pass

    db.session.delete(album)
    db.session.commit()

    socketio.emit("artist_updated", {"artist_id": artist_id})
    return jsonify({"message": f"Album '{album_name}' deleted from discography."})


@app.route("/api/artist/<int:artist_id>/set-deezer-id", methods=["POST"])
def api_artist_set_deezer_id(artist_id):
    artist = db.session.get(Artist, artist_id)
    if not artist:
        return jsonify({"error": "Artist not found"}), 404

    data = request.get_json(silent=True) or {}
    new_deezer_id = str(data.get("deezer_id", "")).strip()
    if not new_deezer_id:
        return jsonify({"error": "Missing deezer_id"}), 400

    artist.spotify_id = new_deezer_id
    db.session.commit()

    opts = {
        "filter_remixes": artist.filter_remixes,
        "filter_lofi": artist.filter_lofi,
        "filter_live": artist.filter_live,
        "filter_compilations": artist.filter_compilations,
        "include_albums": artist.include_albums,
        "include_singles": artist.include_singles,
        "include_compilations": artist.include_compilations,
    }
    socketio.start_background_task(_sync_artist_discography_background, artist.id, int(new_deezer_id), opts)
    return jsonify({"message": f"Artist Deezer ID updated to {new_deezer_id}. Re-syncing discography."})


@app.route("/api/artist/<int:artist_id>/sync", methods=["POST"])
def api_artist_sync(artist_id):
    artist = db.session.get(Artist, artist_id)
    if not artist:
        return jsonify({"error": "Artist not found"}), 404

    deezer_id = int(artist.spotify_id) if artist.spotify_id.isdigit() else None
    if not deezer_id:
        res = search_artist(artist.name, limit=1)
        if res:
            deezer_id = res[0]["id"]

    if not deezer_id:
        return jsonify({"error": "Could not resolve Deezer ID for artist"}), 400

    opts = {
        "filter_remixes": artist.filter_remixes,
        "filter_lofi": artist.filter_lofi,
        "filter_live": artist.filter_live,
        "filter_compilations": artist.filter_compilations,
        "include_albums": artist.include_albums,
        "include_singles": artist.include_singles,
        "include_compilations": artist.include_compilations,
    }

    socketio.start_background_task(_sync_artist_discography_background, artist.id, deezer_id, opts)
    return jsonify({"message": f"Sync started for '{artist.name}'."})


@app.route("/api/artist/<int:artist_id>/monitor", methods=["POST"])
def api_artist_monitor(artist_id):
    artist = db.session.get(Artist, artist_id)
    if not artist:
        return jsonify({"error": "Artist not found"}), 404

    data = request.get_json(silent=True) or {}
    if "monitored" in data:
        artist.monitored = bool(data["monitored"])
    if "auto_download" in data:
        artist.auto_download = bool(data["auto_download"])
    if "filter_remixes" in data:
        artist.filter_remixes = bool(data["filter_remixes"])
    if "filter_lofi" in data:
        artist.filter_lofi = bool(data["filter_lofi"])
    if "filter_live" in data:
        artist.filter_live = bool(data["filter_live"])
    if "filter_compilations" in data:
        artist.filter_compilations = bool(data["filter_compilations"])
    if "include_albums" in data:
        artist.include_albums = bool(data["include_albums"])
    if "include_singles" in data:
        artist.include_singles = bool(data["include_singles"])
    if "include_compilations" in data:
        artist.include_compilations = bool(data["include_compilations"])

    db.session.commit()
    return jsonify({
        "message": "Preferences updated",
        "monitored": artist.monitored,
        "auto_download": artist.auto_download,
        "filter_remixes": artist.filter_remixes,
        "filter_lofi": artist.filter_lofi,
        "filter_live": artist.filter_live,
        "filter_compilations": artist.filter_compilations,
        "include_albums": artist.include_albums,
        "include_singles": artist.include_singles,
        "include_compilations": artist.include_compilations,
    })


@app.route("/api/artist/<int:artist_id>/download-missing", methods=["POST"])
def api_artist_download_missing(artist_id):
    queued = queue_artist_missing(app, artist_id, source="manual")
    return jsonify({"message": f"Queued {queued} missing tracks for download.", "queued_count": queued})


# ══════════════════════════════════════════════════════════════════════
#  Album & Track Actions API
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/album/<int:album_id>/download", methods=["POST"])
def api_album_download(album_id):
    job_ids = queue_album(app, album_id, source="manual")
    return jsonify({"message": f"Queued {len(job_ids)} tracks for album.", "job_ids": job_ids})


@app.route("/api/album/<int:album_id>", methods=["DELETE"])
def api_album_delete(album_id):
    album = db.session.get(Album, album_id)
    if not album:
        return jsonify({"error": "Album not found"}), 404

    del_files = request.args.get("delete_files", "false").lower() == "true"
    name = album.name

    if del_files and album.local_path:
        p = Path(album.local_path)
        if p.is_dir():
            try:
                shutil.rmtree(str(p))
            except OSError:
                pass

    for t in album.tracks.all():
        t.is_downloaded = False
        t.status = "missing"
        t.local_path = None
        t.file_path = ""
        t.size_bytes = 0

    album.is_downloaded = False
    album.size_bytes = 0
    album.local_path = None
    db.session.commit()

    return jsonify({"message": f"Album '{name}' reset/deleted."})


@app.route("/api/track/<int:track_id>/download", methods=["POST"])
def api_track_download(track_id):
    job = queue_track(app, track_id, source="manual")
    if not job:
        return jsonify({"error": "Could not queue track"}), 400
    job_id = getattr(job, "id", None)
    return jsonify({"message": "Track queued for download", "job_id": job_id})


@app.route("/api/track/<int:track_id>", methods=["DELETE"])
def api_track_delete(track_id):
    track = db.session.get(Track, track_id)
    if not track:
        return jsonify({"error": "Track not found"}), 404

    del_files = request.args.get("delete_files", "false").lower() == "true"
    if del_files and track.local_path:
        p = Path(track.local_path)
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass

    track.is_downloaded = False
    track.status = "missing"
    track.local_path = None
    track.file_path = ""
    track.size_bytes = 0
    track.progress = 0.0
    track.error_message = None

    album = track.album
    if album:
        tracks = album.tracks.all()
        album.is_downloaded = all(t.is_downloaded for t in tracks)
        album.size_bytes = sum(t.size_bytes or 0 for t in tracks)

    db.session.commit()
    return jsonify({"message": f"Track '{track.title}' removed from library."})


@app.route("/api/track/<int:track_id>/toggle-monitor", methods=["POST"])
def api_track_toggle_monitor(track_id):
    track = db.session.get(Track, track_id)
    if not track:
        return jsonify({"error": "Track not found"}), 404

    data = request.get_json(silent=True) or {}
    if "monitored" in data:
        track.monitored = bool(data["monitored"])
    else:
        track.monitored = not bool(getattr(track, "monitored", True))

    db.session.commit()
    socketio.emit("track_updated", {"track_id": track.id, "monitored": track.monitored, "artist_id": track.artist_id})
    return jsonify({
        "track_id": track.id,
        "monitored": track.monitored,
        "message": f"Track '{track.title}' {'monitored' if track.monitored else 'disabled/unmonitored'}.",
    })


@app.route("/api/album/<int:album_id>/toggle-monitor", methods=["POST"])
def api_album_toggle_monitor(album_id):
    album = db.session.get(Album, album_id)
    if not album:
        return jsonify({"error": "Album not found"}), 404

    data = request.get_json(silent=True) or {}
    if "monitored" in data:
        album.monitored = bool(data["monitored"])
    else:
        album.monitored = not bool(getattr(album, "monitored", True))

    # Apply to all tracks in this album
    for t in album.tracks.all():
        t.monitored = album.monitored

    db.session.commit()
    socketio.emit("artist_updated", {"artist_id": album.artist_id})
    return jsonify({
        "album_id": album.id,
        "monitored": album.monitored,
        "message": f"Album '{album.name}' {'monitored' if album.monitored else 'disabled/unmonitored'}.",
    })


@app.route("/api/track/<int:track_id>/cancel", methods=["POST"])
def api_track_cancel(track_id):
    job = DownloadJob.query.filter_by(track_id=track_id).filter(DownloadJob.status.in_(["queued", "downloading"])).first()
    if job:
        cancel_job(job.id)
    track = db.session.get(Track, track_id)
    if track:
        track.status = "missing"
        track.progress = 0.0
        db.session.commit()
    return jsonify({"message": "Download cancelled."})


@app.route("/api/track/<int:track_id>/manual-match", methods=["POST"])
def api_track_manual_match(track_id):
    """Manually match and download a track using a user-provided Spotify, YouTube, YouTube Music, or Deezer URL."""
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "Please provide a valid Spotify, YouTube, YouTube Music, or Deezer URL."}), 400

    track = db.session.get(Track, track_id)
    if not track:
        return jsonify({"error": "Track not found"}), 404

    # Trigger manual match download in background task
    socketio.start_background_task(download_manual_match_track, app, socketio, track_id, url)
    return jsonify({
        "message": f"Downloading audio from provided URL for '{track.title}'...",
        "track_id": track_id,
    }), 202


# ══════════════════════════════════════════════════════════════════════
#  Queue & Activity API
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/queue")
def api_get_queue():
    active = DownloadJob.query.filter(DownloadJob.status.in_(["downloading", "queued"])).order_by(DownloadJob.created_at).all()
    history = DownloadJob.query.filter(DownloadJob.status.in_(["completed", "failed", "cancelled"])).order_by(DownloadJob.updated_at.desc()).limit(50).all()

    def _fmt(j):
        art_name = j.artist.name if j.artist else "Unknown Artist"
        track_title = j.track.title if j.track else j.album_name
        return {
            "id": j.id,
            "track_id": j.track_id,
            "album_id": j.album_id,
            "artist_id": j.artist_id,
            "artist_name": art_name,
            "title": track_title,
            "album_name": j.album_name,
            "status": j.status,
            "progress": j.progress,
            "error_message": j.error_message,
            "source": j.source,
            "created_at": j.created_at.isoformat() if j.created_at else None,
        }

    return jsonify({
        "active": [_fmt(j) for j in active],
        "history": [_fmt(j) for j in history],
    })


@app.route("/api/queue/retry-failed", methods=["POST"])
def api_retry_all_failed():
    """Global retry: Re-queues all failed jobs and tracks in the entire library."""
    # 1. Re-queue failed download jobs
    failed_jobs = DownloadJob.query.filter(DownloadJob.status.in_(["failed", "cancelled", "error"])).all()
    for j in failed_jobs:
        j.status = "queued"
        j.progress = 0.0
        j.error_message = None
        if j.track:
            j.track.status = "queued"
            j.track.error_message = None

    # 2. Reset any tracks marked failed without an active job
    failed_tracks = Track.query.filter(
        (Track.status.in_(["failed", "error"])) |
        ((Track.is_downloaded == False) & (Track.error_message.isnot(None)) & (Track.error_message != ""))
    ).all()

    requeued_count = len(failed_jobs)
    for t in failed_tracks:
        t.status = "queued"
        t.error_message = None
        job = DownloadJob.query.filter_by(track_id=t.id).first()
        if job:
            job.status = "queued"
            job.progress = 0.0
            job.error_message = None
        else:
            album = t.album
            artist = album.artist if album else None
            if album and artist:
                db.session.add(DownloadJob(
                    track_id=t.id,
                    album_id=album.id,
                    artist_id=artist.id,
                    item_type="track",
                    album_name=album.name,
                    status="queued",
                    source="retry",
                ))
                requeued_count += 1

    db.session.commit()
    socketio.emit("toast", {"message": f"Re-queued {requeued_count} failed tracks.", "type": "info"})
    return jsonify({"message": f"Re-queued {requeued_count} failed tracks.", "count": requeued_count})


@app.route("/api/jobs/<int:job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    cancel_job(job_id)
    return jsonify({"message": f"Job {job_id} cancellation requested."})


@app.route("/api/jobs/<int:job_id>/retry", methods=["POST"])
def api_job_retry(job_id):
    j = db.session.get(DownloadJob, job_id)
    if not j:
        return jsonify({"error": "Job not found"}), 404
    j.status = "queued"
    j.progress = 0.0
    j.error_message = None
    if j.track:
        j.track.status = "queued"
        j.track.error_message = None
    db.session.commit()
    return jsonify({"message": f"Job {job_id} re-queued."})


@app.route("/api/jobs/<int:job_id>", methods=["DELETE"])
def api_job_delete(job_id):
    j = db.session.get(DownloadJob, job_id)
    if not j:
        return jsonify({"error": "Job not found"}), 404
    cancel_job(job_id)
    db.session.delete(j)
    db.session.commit()
    return jsonify({"message": f"Job {job_id} deleted."})


# ══════════════════════════════════════════════════════════════════════
#  Interactive Root Folder Import API
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/import/candidates")
def api_import_candidates():
    music_path = _get_setting("music_path", "/music")
    candidates = scan_root_folder_candidates(music_path)
    return jsonify(candidates)


@app.route("/api/import/folder", methods=["POST"])
def api_import_folder():
    data = request.get_json(silent=True) or {}
    folder_name = data.get("folder_name")
    deezer_id = data.get("deezer_id")
    if not folder_name:
        return jsonify({"error": "Folder name required"}), 400

    music_path = _get_setting("music_path", "/music")
    res = import_artist_folder(music_path, folder_name, deezer_artist_id=deezer_id, filter_options=data)
    if "error" in res:
        return jsonify(res), 400

    socketio.emit("artist_added", {"artist_id": res["artist_id"]})
    return jsonify(res)


# ══════════════════════════════════════════════════════════════════════
#  Navidrome Subsonic Integration API
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/navidrome/test", methods=["POST"])
def api_navidrome_test():
    data = request.get_json(silent=True) or {}
    url = data.get("url") or _get_setting("navidrome_url")
    user = data.get("user") or _get_setting("navidrome_user")
    token = data.get("token") or _get_setting("navidrome_token")
    ok, msg = test_navidrome_connection(url, user, token)
    return jsonify({"success": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/navidrome/scan", methods=["POST"])
def api_navidrome_scan():
    ok, msg = trigger_navidrome_scan(app)
    return jsonify({"success": ok, "message": msg}), (200 if ok else 400)


# ══════════════════════════════════════════════════════════════════════
#  Cookies Management API
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/cookies/status", methods=["GET"])
def api_cookies_status():
    custom_path = _get_setting("youtube_cookies_path", "/config/cookies.txt")
    return jsonify(get_cookies_status(custom_path))


@app.route("/api/cookies/upload", methods=["POST"])
def api_cookies_upload():
    """Upload or paste a cookies.txt file for yt-dlp authentication."""
    custom_path = _get_setting("youtube_cookies_path", "/config/cookies.txt")
    dest = Path(custom_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if "file" in request.files:
        f = request.files["file"]
        if f.filename == "":
            return jsonify({"error": "No file selected"}), 400
        f.save(str(dest))
    else:
        data = request.get_json(silent=True) or {}
        content = data.get("content", "")
        if not content.strip():
            return jsonify({"error": "No cookies content provided"}), 400
        dest.write_text(content.strip(), encoding="utf-8")

    st = get_cookies_status(str(dest))
    return jsonify({
        "message": f"cookies.txt saved successfully ({st.get('cookie_count', 0)} cookies detected).",
        "status": st,
    })


@app.route("/api/cookies/delete", methods=["POST", "DELETE"])
def api_cookies_delete():
    """Delete the active cookies.txt file."""
    custom_path = _get_setting("youtube_cookies_path", "/config/cookies.txt")
    cp = get_cookies_path(custom_path)
    if cp and cp.exists():
        try:
            cp.unlink()
        except OSError as e:
            return jsonify({"error": f"Failed to delete cookies file: {e}"}), 500
    return jsonify({
        "message": "cookies.txt file deleted.",
        "status": get_cookies_status(custom_path),
    })


# ══════════════════════════════════════════════════════════════════════
#  Settings API
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if "max_concurrent" in data:
            v = max(1, min(int(data["max_concurrent"]), 10))
            _set_setting("max_concurrent", str(v))
        if "spotiflac_quality" in data:
            _set_setting("spotiflac_quality", data["spotiflac_quality"])
        if "spotiflac_delay" in data:
            _set_setting("spotiflac_delay", str(max(0.5, float(data["spotiflac_delay"]))))
        if "youtube_source" in data:
            _set_setting("youtube_source", str(data["youtube_source"]).strip())
        if "youtube_cookies_path" in data:
            _set_setting("youtube_cookies_path", str(data["youtube_cookies_path"]).strip())
        if "ytdlp_format" in data:
            _set_setting("ytdlp_format", data["ytdlp_format"])
            _set_setting("spotdl_format", data["ytdlp_format"])
        elif "spotdl_format" in data:
            _set_setting("ytdlp_format", data["spotdl_format"])
            _set_setting("spotdl_format", data["spotdl_format"])
        if "spotdl_source" in data:
            _set_setting("spotdl_source", data["spotdl_source"])
        if "discography_interval_hours" in data:
            _set_setting("discography_interval_hours", str(max(1, int(data["discography_interval_hours"]))))
        if "music_path" in data:
            _set_setting("music_path", data["music_path"])
        if "enable_spotiflac" in data:
            _set_setting("enable_spotiflac", "true" if data["enable_spotiflac"] else "false")
        if "enable_ytdlp" in data:
            _set_setting("enable_ytdlp", "true" if data["enable_ytdlp"] else "false")
        if "save_cover_art" in data:
            _set_setting("save_cover_art", "true" if data["save_cover_art"] else "false")
        if "cover_art_filename" in data:
            _set_setting("cover_art_filename", str(data["cover_art_filename"]))
        if "embed_cover_art" in data:
            _set_setting("embed_cover_art", "true" if data["embed_cover_art"] else "false")
        if "matching_strictness" in data:
            _set_setting("matching_strictness", str(data["matching_strictness"]))
        if "reject_mismatches" in data:
            _set_setting("reject_mismatches", "true" if data["reject_mismatches"] else "false")
        if "enable_duration_check" in data:
            _set_setting("enable_duration_check", "true" if data["enable_duration_check"] else "false")
        if "enable_folder_watcher" in data:
            _set_setting("enable_folder_watcher", "true" if data["enable_folder_watcher"] else "false")
        if "navidrome_url" in data:
            _set_setting("navidrome_url", str(data["navidrome_url"]).strip())
        if "navidrome_user" in data:
            _set_setting("navidrome_user", str(data["navidrome_user"]).strip())
        if "navidrome_token" in data:
            _set_setting("navidrome_token", str(data["navidrome_token"]).strip())
        if "navidrome_auto_scan" in data:
            _set_setting("navidrome_auto_scan", "true" if data["navidrome_auto_scan"] else "false")
        if "theme" in data:
            _set_setting("theme", str(data["theme"]).strip())
        if "spotify_client_id" in data:
            _set_setting("spotify_client_id", str(data["spotify_client_id"]).strip())
        if "spotify_client_secret" in data:
            _set_setting("spotify_client_secret", str(data["spotify_client_secret"]).strip())
        return jsonify({"message": "Settings updated successfully."})

    fallback_fmt = _get_setting("ytdlp_format") or _get_setting("spotdl_format", "opus")
    cookies_path = _get_setting("youtube_cookies_path", "/config/cookies.txt")

    return jsonify({
        "version": __version__,
        "max_concurrent": int(_get_setting("max_concurrent", str(app.config["MAX_CONCURRENT_DEFAULT"]))),
        "api_key": get_api_key(app),
        "theme": _get_setting("theme", "onyx-dark"),
        "spotiflac_quality": _get_setting("spotiflac_quality", "LOSSLESS"),
        "spotiflac_delay": float(_get_setting("spotiflac_delay", "3.0")),
        "spotify_client_id": _get_setting("spotify_client_id", ""),
        "spotify_client_secret": _get_setting("spotify_client_secret", ""),
        "youtube_source": _get_setting("youtube_source", "youtube_music"),
        "youtube_cookies_path": cookies_path,
        "cookies_status": get_cookies_status(cookies_path),
        "ytdlp_format": fallback_fmt,
        "spotdl_format": fallback_fmt,
        "spotdl_source": _get_setting("spotdl_source", "youtube"),
        "enable_spotiflac": _get_setting("enable_spotiflac", "true").lower() == "true",
        "enable_ytdlp": _get_setting("enable_ytdlp", "true").lower() == "true",
        "discography_interval_hours": int(_get_setting("discography_interval_hours", "6")),
        "music_path": _get_setting("music_path", "/music"),
        "save_cover_art": _get_setting("save_cover_art", "true").lower() == "true",
        "cover_art_filename": _get_setting("cover_art_filename", "cover.jpg"),
        "embed_cover_art": _get_setting("embed_cover_art", "true").lower() == "true",
        "matching_strictness": _get_setting("matching_strictness", "standard"),
        "reject_mismatches": _get_setting("reject_mismatches", "true").lower() == "true",
        "enable_duration_check": _get_setting("enable_duration_check", "true").lower() == "true",
        "enable_folder_watcher": _get_setting("enable_folder_watcher", "true").lower() == "true",
        "navidrome_url": _get_setting("navidrome_url", ""),
        "navidrome_user": _get_setting("navidrome_user", ""),
        "navidrome_token": _get_setting("navidrome_token", ""),
        "navidrome_auto_scan": _get_setting("navidrome_auto_scan", "true").lower() == "true",
    })


@app.route("/api/version", methods=["GET"])
def api_version():
    return jsonify({"version": __version__})


@app.route("/api/settings/rotate-key", methods=["POST"])
def api_rotate_key():
    k = secrets.token_hex(16)
    _set_setting("api_key", k)
    return jsonify({"message": "API key rotated. Update Lidarr.", "api_key": k})


# ══════════════════════════════════════════════════════════════════════
#  Lidarr Integration Endpoints
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/sabnzbd", methods=["GET", "POST"])
@app.route("/api/sabnzbd/api", methods=["GET", "POST"])
@app.route("/sabnzbd/api", methods=["GET", "POST"])
def sabnzbd_proxy():
    return handle_sabnzbd_api(app)


@app.route("/api/newznab", methods=["GET"])
@app.route("/api/newznab/api", methods=["GET"])
@app.route("/api/torznab", methods=["GET"])
def newznab_proxy():
    return handle_newznab_api(app)


@app.route("/api/nzb/<item_type>/<int:item_id>", methods=["GET"])
def api_nzb_grab(item_type, item_id):
    return handle_newznab_api(app)


# ══════════════════════════════════════════════════════════════════════
#  Socket.IO Real-time Events
# ══════════════════════════════════════════════════════════════════════

@socketio.on("connect")
def handle_socket_connect():
    emit("connected", {"status": "ok"})


# ══════════════════════════════════════════════════════════════════════
#  Background Workers & Schedulers
# ══════════════════════════════════════════════════════════════════════

def _periodic_discography_sync_loop():
    """Periodic auto-sync for monitored artists based on configured interval."""
    gevent.sleep(60)
    while True:
        try:
            with app.app_context():
                interval_h = int(_get_setting("discography_interval_hours", "6"))
                now = datetime.now(timezone.utc)
                monitored_artists = Artist.query.filter_by(monitored=True).all()
                artists_to_sync = []
                for a in monitored_artists:
                    if a.sync_status == "syncing":
                        continue
                    if a.last_synced_at:
                        last = a.last_synced_at
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                        elapsed_h = (now - last).total_seconds() / 3600.0
                        if elapsed_h < interval_h:
                            continue
                    artists_to_sync.append((a.id, a.spotify_id, {
                        "filter_remixes": a.filter_remixes,
                        "filter_lofi": a.filter_lofi,
                        "filter_live": a.filter_live,
                        "filter_compilations": a.filter_compilations,
                        "include_albums": a.include_albums,
                        "include_singles": a.include_singles,
                        "include_compilations": a.include_compilations,
                    }))

            if artists_to_sync:
                logger.info("[SCHEDULER] Running periodic discography check for %d monitored artists", len(artists_to_sync))
                for aid, spot_id, opts in artists_to_sync:
                    deezer_id = int(spot_id) if spot_id.isdigit() else None
                    if deezer_id:
                        _sync_artist_discography_background(aid, deezer_id, opts)
                    gevent.sleep(3)

        except Exception:
            logger.exception("[SCHEDULER] Periodic discography check failed")

        # Periodically checkpoint SQLite WAL log to keep disk space and startup I/O optimal
        try:
            with app.app_context():
                with db.engine.connect() as conn:
                    conn.execute(db.text("PRAGMA wal_checkpoint(PASSIVE)"))
        except Exception:
            pass

        gevent.sleep(300)


# Database initialization and startup
with app.app_context():
    db.create_all()
    # Create SQLite performance indexes & schema migrations
    try:
        with db.engine.connect() as conn:
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_artists_name ON artists (name)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_albums_artist_id ON albums (artist_id)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_tracks_album_id ON tracks (album_id)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_tracks_artist_id ON tracks (artist_id)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_tracks_is_downloaded ON tracks (is_downloaded)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks (status)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks (isrc)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_jobs_status ON download_jobs (status)"))

            # Safe column additions for existing databases
            try:
                conn.execute(db.text("ALTER TABLE albums ADD COLUMN monitored BOOLEAN DEFAULT 1"))
            except Exception:
                pass
            try:
                conn.execute(db.text("ALTER TABLE tracks ADD COLUMN monitored BOOLEAN DEFAULT 1"))
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass

    # Ensure default settings
    default_settings = [
        ("max_concurrent", str(app.config["MAX_CONCURRENT_DEFAULT"])),
        ("spotiflac_quality", "LOSSLESS"),
        ("spotiflac_delay", "3.0"),
        ("youtube_source", "youtube_music"),
        ("youtube_cookies_path", "/config/cookies.txt"),
        ("spotdl_format", "opus"),
        ("ytdlp_format", "opus"),
        ("spotdl_source", "youtube"),
        ("music_path", "/music"),
        ("enable_spotiflac", "true"),
        ("enable_ytdlp", "true"),
        ("save_cover_art", "true"),
        ("cover_art_filename", "cover.jpg"),
        ("embed_cover_art", "true"),
        ("matching_strictness", "standard"),
        ("reject_mismatches", "true"),
        ("enable_duration_check", "true"),
        ("enable_folder_watcher", "true"),
        ("navidrome_auto_scan", "true"),
        ("theme", "onyx-dark"),
    ]
    for k, v in default_settings:
        if not db.session.get(AppSetting, k):
            db.session.add(AppSetting(key=k, value=v))
    db.session.commit()
    get_api_key(app)

# Start background services
socketio.start_background_task(start_queue_worker, app, socketio)
socketio.start_background_task(start_folder_watcher, app, socketio)
socketio.start_background_task(_periodic_discography_sync_loop)
logger.info("fnack server initialized and background tasks (queue, watcher, scheduler) started")

if __name__ == "__main__":
    logger.info("Starting fnack on 0.0.0.0:4688")
    socketio.run(app, host="0.0.0.0", port=4688, debug=False, allow_unsafe_werkzeug=True)

