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
            "file_path": t.file_path, "duration": t.duration, "bitrate": t.bitrate,
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
        self._permissions = set(permissions)
        self.downloads_dir = Path("/downloads")
        self.music_dir = Path("/music")
        self.data_dir = Path("/config/plugins") / plugin_id / "data"
        if "filesystem:downloads" in self._permissions or "filesystem:music" in self._permissions:
            self.data_dir.mkdir(parents=True, exist_ok=True)

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
