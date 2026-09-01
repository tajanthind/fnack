"""The PluginContext: the only thing a plugin instance is ever given.

Every sub-facade here is intentionally narrow. When you're tempted to widen
one (e.g. "just expose db.session"), don't — that reintroduces the coupling
this whole framework exists to remove. Add a specific method instead
(`LibraryContext.mark_caution(...)` rather than raw session access).

This module imports fnack's real `models` lazily (inside functions) so the
`plugins` package has no import-time dependency on the rest of the app and
can be unit-tested in isolation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import requests

from plugins.events import EventBus

log = logging.getLogger("fnack.plugins.context")


# --------------------------------------------------------------------------
# Library access — the ORM-insulation layer
# --------------------------------------------------------------------------

class LibraryContext:
    """Read-mostly, method-based access to the music library. Plugins never
    see a SQLAlchemy model or session; if a plugin needs a new capability,
    add a method here rather than widening access."""

    def get_track(self, track_id: int) -> Optional[dict]:
        from models import Track, db  # local import: avoids a hard dependency at package import time
        t = db.session.get(Track, track_id)
        if not t:
            return None
        return {
            "id": t.id, "title": t.title, "isrc": t.isrc, "status": t.status,
            "file_path": t.file_path, "local_path": t.local_path,
            "duration": t.duration, "bitrate": t.bitrate,
            "caution": t.caution, "caution_info": t.caution_info,
        }

    def get_album(self, album_id: int) -> Optional[dict]:
        from models import Album, db
        a = db.session.get(Album, album_id)
        if not a:
            return None
        return {"id": a.id, "name": a.name, "year": a.year, "is_downloaded": a.is_downloaded}

    def get_artist(self, artist_id: int) -> Optional[dict]:
        from models import Artist, db
        a = db.session.get(Artist, artist_id)
        if not a:
            return None
        return {"id": a.id, "name": a.name, "monitored": a.monitored}

    def list_missing_tracks(self, limit: int = 500) -> list[dict]:
        from models import Track
        rows = Track.query.filter_by(status="missing").limit(limit).all()
        return [{"id": t.id, "title": t.title, "isrc": t.isrc} for t in rows]

    def list_artists(self) -> list[dict]:
        """All artists (for Subsonic-style server extensions)."""
        from models import Artist
        return [{"id": a.id, "name": a.name, "image_url": a.image_url}
                for a in Artist.query.order_by(Artist.name).all()]

    def list_albums(self, artist_id: Optional[int] = None, limit: int = 500) -> list[dict]:
        """Albums, optionally filtered by artist (Subsonic album list)."""
        from models import Album
        q = Album.query
        if artist_id:
            q = q.filter_by(artist_id=artist_id)
        rows = q.order_by(Album.name).limit(limit).all()
        return [{"id": a.id, "name": a.name, "year": a.year,
                 "artist_id": a.artist_id, "cover_url": a.cover_url,
                 "is_downloaded": a.is_downloaded} for a in rows]

    def list_tracks(self, album_id: Optional[int] = None, limit: int = 1000) -> list[dict]:
        """Tracks, optionally filtered by album (Subsonic song list)."""
        from models import Track
        q = Track.query
        if album_id:
            q = q.filter_by(album_id=album_id)
        rows = q.order_by(Track.disc_number, Track.track_number).limit(limit).all()
        return [{"id": t.id, "title": t.title, "album_id": t.album_id,
                 "artist_id": t.artist_id, "track_number": t.track_number,
                 "disc_number": t.disc_number, "duration": t.duration,
                 "file_path": t.file_path, "local_path": t.local_path,
                 "is_downloaded": t.is_downloaded, "bitrate": t.bitrate,
                 "size_bytes": t.size_bytes} for t in rows]

    def get_setting(self, key: str, default=None) -> Optional[str]:
        """Read a core AppSetting value (or default)."""
        from models import AppSetting, db
        row = db.session.get(AppSetting, key)
        return row.value if row else default

    def set_setting(self, key: str, value) -> None:
        """Write a core AppSetting value."""
        from models import AppSetting, db
        row = db.session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=str(value))
            db.session.add(row)
        else:
            row.value = str(value)
        db.session.commit()

    def get_api_key(self) -> str:
        """The configured M2M API key ('' if unset). Exposed so server-
        extension plugins (e.g. Subsonic) can authenticate clients against
        the same key without touching models directly."""
        return self.get_setting("api_key", "").strip()

    def get_or_create_api_key(self) -> str:
        """The M2M API key, generating + persisting one if none is set
        (moved from the former `services/lidarr_service.py` — Lidarr-style
        integrations authenticate against this key). fnack stays fully open
        (zero required auth) until a key is set."""
        import secrets
        key = self.get_setting("api_key", "").strip()
        if key:
            return key
        key = secrets.token_hex(16)
        self.set_setting("api_key", key)
        return key

    def search_albums(self, query: str, limit: int = 10) -> list[dict]:
        """Live album search (deezer, core-direct). Same underlying function
        the interactive search endpoint uses — the confirmed search split
        keeps interactive/search paths core, calling the bundled provider
        directly rather than through the plugin chain.

        NOTE (Phase 1.1 review §3): this is a KNOWN Deezer-hardwired boundary
        inside the supposedly-generic PluginContext (it imports
        services.deezer_service). It exists so the Lidarr plugin can search
        without reaching into services. Phase 2 removes it: the Lidarr plugin
        will call the metadata capability instead, and PluginContext stays
        provider-generic. Do NOT "solve" this by adding more generic-looking
        Deezer methods here."""
        from services.deezer_service import search_album
        return search_album(query, limit=limit)

    def search_tracks(self, query: str, limit: int = 10) -> list[dict]:
        """Live track search (deezer, core-direct — see search_albums)."""
        from services.deezer_service import search_track
        return search_track(query, limit=limit)

    def get_album_info(self, album_id: int) -> dict:
        """Deezer album metadata (core-direct; used by the Lidarr plugin to
        build friendly release names)."""
        from services.deezer_service import get_album_info
        return get_album_info(album_id)

    def get_track_info(self, track_id: int) -> dict:
        """Deezer track metadata (core-direct; see get_album_info)."""
        from services.deezer_service import get_track_info
        return get_track_info(track_id)

    def queue_lidarr_grab(self, item_type: str, item_id: int) -> list[int]:
        """Expand a Lidarr grab (a Deezer album or track id) into the local
        library — creates Artist / Album / Track rows and queues one
        DownloadJob per track so the queue worker downloads them like any
        other track. Returns the created/queued job ids (moved verbatim from
        the former `services/lidarr_service.py::_create_lidarr_grab_job`)."""
        from services.deezer_service import get_album_tracks, get_album_info, get_track_info
        from models import Album, Artist, DownloadJob, Track, db

        job_ids: list = []

        if item_type == "track":
            info = get_track_info(item_id)
            artist_name = info.get("artist_name") or "Unknown Artist"
            track_title = info.get("title") or "Unknown Track"
            album_title = info.get("album_title") or track_title
            album_deezer_id = info.get("album_id")
            cover_url = None
            year = None
            record_type = "single"
            tracks_to_queue = [{
                "id": item_id,
                "title": track_title,
                "track_position": 1,
                "disk_number": 1,
                "duration": float(info.get("duration") or 0),
            }]
        else:
            info = get_album_info(item_id)
            artist_name = info.get("artist_name") or "Unknown Artist"
            album_title = info.get("title") or "Unknown Album"
            album_deezer_id = info.get("id") or item_id
            cover_url = info.get("cover_url")
            year = info.get("year")
            record_type = info.get("record_type") or "album"
            tracks_to_queue = get_album_tracks(item_id)

        if not tracks_to_queue:
            log.warning("[LIDARR] Grab for %s %d returned no tracks", item_type, item_id)
            return []

        artist = Artist.query.filter_by(name=artist_name).first()
        if not artist:
            artist = Artist(
                spotify_id=f"lidarr:{artist_name}",
                name=artist_name,
                source="lidarr",
                monitored=True,
            )
            db.session.add(artist)
            db.session.flush()

        album = None
        if album_deezer_id:
            album = Album.query.filter_by(artist_id=artist.id, deezer_id=str(album_deezer_id)).first()
        if not album:
            album = Album(
                artist_id=artist.id,
                name=album_title,
                year=year,
                cover_url=cover_url,
                deezer_id=str(album_deezer_id) if album_deezer_id else None,
                record_type=record_type,
            )
            db.session.add(album)
            db.session.flush()

        for t in tracks_to_queue:
            track = Track.query.filter_by(album_id=album.id, deezer_id=str(t["id"])).first()
            if not track:
                track = Track(
                    album_id=album.id,
                    artist_id=artist.id,
                    title=t["title"],
                    track_number=t.get("track_position") or 0,
                    disc_number=t.get("disk_number") or 1,
                    duration=t.get("duration"),
                    deezer_id=str(t["id"]),
                    status="missing",
                )
                db.session.add(track)
                db.session.flush()

            existing = DownloadJob.query.filter_by(track_id=track.id, status="queued").first()
            if existing:
                job_ids.append(existing.id)
                continue

            job = DownloadJob(
                track_id=track.id,
                album_id=album.id,
                artist_id=artist.id,
                item_type="track",
                album_spotify_id=str(album.deezer_id or ""),
                album_name=album.name,
                album_type=album.record_type,
                album_url="",
                cover_url=album.cover_url,
                status="queued",
                source="lidarr",
            )
            track.status = "queued"
            db.session.add(job)
            db.session.flush()
            job_ids.append(job.id)

        album.is_downloaded = False
        db.session.commit()
        log.info("[LIDARR] Grab expanded: %d track job(s) queued for '%s - %s'", len(job_ids), artist_name, album_title)
        return job_ids

    def list_download_jobs(self, statuses: list) -> list[dict]:
        """DownloadJobs in the given statuses, as plain dicts (SABnzbd
        queue/history emulation)."""
        from models import DownloadJob
        jobs = DownloadJob.query.filter(DownloadJob.status.in_(statuses)).all()
        return [{
            "id": j.id,
            "album_name": j.album_name,
            "status": j.status,
            "progress": j.progress,
            "artist_name": j.artist.name if j.artist else "Unknown",
            "source": j.source,
        } for j in jobs]

    def cancel_download_job(self, job_id: int) -> bool:
        """Cancel a DownloadJob (SABnzbd delete emulation)."""
        from models import DownloadJob, db
        j = db.session.get(DownloadJob, job_id)
        if not j:
            return False
        j.status = "cancelled"
        db.session.commit()
        return True

    def update_track_status(self, track_id: int, status: str, error_message: str = None) -> None:
        from models import Track, db
        t = db.session.get(Track, track_id)
        if not t:
            return
        t.status = status
        if error_message is not None:
            t.error_message = error_message
        db.session.commit()

    def mark_caution(self, track_id: int, reason: str) -> None:
        """Flag a track for user attention without changing its download
        status or deleting anything — generalizes the AcoustID low-confidence
        caution flag into something any plugin can set."""
        from models import Track, db
        t = db.session.get(Track, track_id)
        if not t:
            return
        t.caution = True
        t.caution_info = reason
        db.session.commit()


# --------------------------------------------------------------------------
# Per-plugin namespaced settings
# --------------------------------------------------------------------------

class SettingsContext:
    def __init__(self, plugin_id: str):
        self._plugin_id = plugin_id

    def get(self, key: str, default=None):
        from plugins.models import PluginSetting
        from models import db
        row = db.session.get(PluginSetting, (self._plugin_id, key))
        return row.value if row else default

    def set(self, key: str, value) -> None:
        from plugins.models import PluginSetting
        from models import db
        row = db.session.get(PluginSetting, (self._plugin_id, key))
        if row is None:
            row = PluginSetting(plugin_id=self._plugin_id, key=key, value=str(value))
            db.session.add(row)
        else:
            row.value = str(value)
        db.session.commit()

    def all(self) -> dict:
        from plugins.models import PluginSetting
        rows = PluginSetting.query.filter_by(plugin_id=self._plugin_id).all()
        return {r.key: r.value for r in rows}


# --------------------------------------------------------------------------
# Sandboxed filesystem access
# --------------------------------------------------------------------------

class FSContext:
    """Restricts a plugin to fnack's own directories. `data_dir` is a
    private per-plugin scratch space nothing else can see."""

    def __init__(self, plugin_id: str, permissions: list[str]):
        import os
        self._permissions = set(permissions)
        self.downloads_dir = Path(os.environ.get("DOWNLOADS_DIR", "/downloads"))
        self.music_dir = Path(os.environ.get("MUSIC_DIR", "/music"))
        config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
        self.data_dir = config_dir / "plugins" / plugin_id / "data"
        if "filesystem:downloads" in self._permissions or "filesystem:music" in self._permissions:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def _check(self, permission: str):
        if permission not in self._permissions:
            raise PermissionError(
                f"plugin did not declare '{permission}' in its manifest permissions"
            )

    def open_download_path(self, relative: str):
        self._check("filesystem:downloads")
        return self.downloads_dir / relative

    def open_data_path(self, relative: str):
        return self.data_dir / relative


# --------------------------------------------------------------------------
# Events — thin per-plugin wrapper around the shared EventBus
# --------------------------------------------------------------------------

class EventsContext:
    """Auto-tags subscriptions with the owning plugin's id so
    PluginManager can unsubscribe everything for a plugin in one call on
    disable/unload — plugin authors never need to track callbacks themselves."""

    def __init__(self, plugin_id: str, bus: EventBus):
        self._plugin_id = plugin_id
        self._bus = bus

    def subscribe(self, event_name: str, callback: Callable[..., None]) -> None:
        self._bus.subscribe(event_name, callback, plugin_id=self._plugin_id)

    def unsubscribe(self, event_name: str, callback: Callable[..., None]) -> None:
        self._bus.unsubscribe(event_name, callback)

    def emit(self, event_name: str, **payload) -> None:
        self._bus.emit(event_name, **payload)


# --------------------------------------------------------------------------
# UI slot registration
# --------------------------------------------------------------------------

class UIContext:
    def __init__(self, plugin_id: str, slot_registry: dict[str, list]):
        self._plugin_id = plugin_id
        self._slot_registry = slot_registry

    def register_slot(self, slot_name: str, render_fn: Callable[[dict], str]) -> None:
        """`render_fn(context_data) -> html_fragment`. Called by core's
        `plugin_slot()` Jinja helper for every enabled contributor of a slot."""
        self._slot_registry.setdefault(slot_name, []).append((self._plugin_id, render_fn))


# --------------------------------------------------------------------------
# Background scheduling (thin wrapper; core decides the actual executor)
# --------------------------------------------------------------------------

class JobsContext:
    def __init__(self, scheduler_hook: Callable[[float, Callable[[], None]], None]):
        self._scheduler_hook = scheduler_hook

    def schedule_interval(self, seconds: float, fn: Callable[[], None]) -> None:
        self._scheduler_hook(seconds, fn)


# --------------------------------------------------------------------------
# The bundle handed to a plugin's __init__
# --------------------------------------------------------------------------

class PluginContext:
    def __init__(
        self,
        plugin_id: str,
        permissions: list[str],
        event_bus: EventBus,
        ui_slot_registry: dict[str, list],
        scheduler_hook: Callable[[float, Callable[[], None]], None],
    ):
        self.library = LibraryContext()
        self.settings = SettingsContext(plugin_id)
        self.events = EventsContext(plugin_id, event_bus)
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": f"fnack-plugin/{plugin_id}"})
        self.fs = FSContext(plugin_id, permissions)
        self.ui = UIContext(plugin_id, ui_slot_registry)
        self.jobs = JobsContext(scheduler_hook)
        self.log = logging.getLogger(f"fnack.plugin.{plugin_id}")
