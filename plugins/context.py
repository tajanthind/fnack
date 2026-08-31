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
            "album_id": t.album_id, "artist_id": t.artist_id,
            "track_number": t.track_number, "disc_number": t.disc_number,
            "size_bytes": t.size_bytes, "genre": t.genre,
            "file_format": t.file_format, "created_at": t.created_at,
        }

    def get_album(self, album_id: int) -> Optional[dict]:
        from models import Album, db
        a = db.session.get(Album, album_id)
        if not a:
            return None
        return {"id": a.id, "name": a.name, "year": a.year,
                "is_downloaded": a.is_downloaded, "artist_id": a.artist_id,
                "cover_url": a.cover_url, "local_path": a.local_path}

    def get_artist(self, artist_id: int) -> Optional[dict]:
        from models import Artist, db
        a = db.session.get(Artist, artist_id)
        if not a:
            return None
        return {"id": a.id, "name": a.name, "monitored": a.monitored,
                "image_url": a.image_url}

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
                 "is_downloaded": a.is_downloaded, "local_path": a.local_path,
                 "created_at": a.created_at} for a in rows]

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
                 "size_bytes": t.size_bytes, "genre": t.genre,
                 "file_format": t.file_format, "created_at": t.created_at} for t in rows]

    def search_library(self, query: str, artist_limit: int = 20,
                       album_limit: int = 20, track_limit: int = 20) -> dict:
        """Case-insensitive substring search across artists, albums and
        tracks (Subsonic search2/search3). Rows use the same shapes as
        list_artists/list_albums/list_tracks."""
        from models import Album, Artist, Track
        like = f"%{(query or '').strip()}%"
        if like == "%%":
            return {"artists": [], "albums": [], "tracks": []}
        artists = (Artist.query.filter(Artist.name.ilike(like))
                   .order_by(Artist.name).limit(artist_limit).all())
        albums = (Album.query.filter(Album.name.ilike(like))
                  .order_by(Album.name).limit(album_limit).all())
        tracks = (Track.query.filter(Track.title.ilike(like))
                  .order_by(Track.title).limit(track_limit).all())
        return {
            "artists": [{"id": a.id, "name": a.name, "image_url": a.image_url}
                        for a in artists],
            "albums": [{"id": al.id, "name": al.name, "year": al.year,
                        "artist_id": al.artist_id, "cover_url": al.cover_url,
                        "is_downloaded": al.is_downloaded, "local_path": al.local_path,
                        "created_at": al.created_at} for al in albums],
            "tracks": [{"id": t.id, "title": t.title, "album_id": t.album_id,
                        "artist_id": t.artist_id, "track_number": t.track_number,
                        "disc_number": t.disc_number, "duration": t.duration,
                        "file_path": t.file_path, "local_path": t.local_path,
                        "is_downloaded": t.is_downloaded, "bitrate": t.bitrate,
                        "size_bytes": t.size_bytes, "genre": t.genre,
                        "file_format": t.file_format, "created_at": t.created_at}
                       for t in tracks],
        }

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
