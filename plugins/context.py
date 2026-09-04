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


class PermissionChecker:
    """Fail-closed manifest permission gate shared by every context facade.

    A plugin may only touch a facade (network, settings, library reads or
    writes, sandboxed filesystem paths) when its plugin.json declares the
    matching permission — the manifest is a real contract, not documentation.
    """

    def __init__(self, plugin_id: str, permissions: list[str] | None):
        self._plugin_id = plugin_id
        self._permissions = set(permissions or [])

    def __call__(self, permission: str) -> None:
        if permission not in self._permissions:
            raise PermissionError(
                f"plugin '{self._plugin_id}' did not declare '{permission}' "
                f"in its manifest permissions"
            )


# --------------------------------------------------------------------------
# Library access — the ORM-insulation layer
# --------------------------------------------------------------------------

def core_context_checker() -> PermissionChecker:
    """A checker that allows everything — for CORE code acting on behalf of
    the app (seeding keys etc.), never for plugin code."""
    return PermissionChecker("fnack-core", [
        "network", "settings", "library:read", "library:write",
        "filesystem:music", "filesystem:downloads",
    ])


class LibraryContext:
    def __init__(self, checker: Optional[PermissionChecker] = None):
        # Fail closed: a context built without an explicit checker permits
        # nothing (only core_context_checker()/PluginContext should construct
        # these).
        self._check = checker or PermissionChecker("untrusted", [])
    """Read-mostly, method-based access to the music library. Plugins never
    see a SQLAlchemy model or session; if a plugin needs a new capability,
    add a method here rather than widening access."""

    def get_track(self, track_id: int) -> Optional[dict]:
        self._check("library:read")
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
        self._check("library:read")
        from models import Album, db
        a = db.session.get(Album, album_id)
        if not a:
            return None
        return {"id": a.id, "name": a.name, "year": a.year, "is_downloaded": a.is_downloaded}

    def get_artist(self, artist_id: int) -> Optional[dict]:
        self._check("library:read")
        from models import Artist, db
        a = db.session.get(Artist, artist_id)
        if not a:
            return None
        return {"id": a.id, "name": a.name, "monitored": a.monitored}

    def list_missing_tracks(self, limit: int = 500) -> list[dict]:
        self._check("library:read")
        from models import Track
        rows = Track.query.filter_by(status="missing").limit(limit).all()
        return [{"id": t.id, "title": t.title, "isrc": t.isrc} for t in rows]

    def list_artists(self) -> list[dict]:
        """All artists (for Subsonic-style server extensions)."""
        self._check("library:read")
        from models import Artist
        return [{"id": a.id, "name": a.name, "image_url": a.image_url}
                for a in Artist.query.order_by(Artist.name).all()]

    def list_albums(self, artist_id: Optional[int] = None, limit: int = 500) -> list[dict]:
        """Albums, optionally filtered by artist (Subsonic album list)."""
        self._check("library:read")
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
        self._check("library:read")
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
        self._check("library:read")
        from models import AppSetting, db
        row = db.session.get(AppSetting, key)
        return row.value if row else default

    def set_setting(self, key: str, value) -> None:
        """Write a core AppSetting value."""
        self._check("library:write")
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
        self._check("library:read")
        return self.get_setting("api_key", "").strip()

    def get_or_create_api_key(self) -> str:
        """The M2M API key, generating + persisting one if none is set
        (moved from the former `services/lidarr_service.py` — Lidarr-style
        integrations authenticate against this key). fnack stays fully open
        (zero required auth) until a key is set."""
        self._check("library:write")
        import secrets
        key = self.get_setting("api_key", "").strip()
        if key:
            return key
        key = secrets.token_hex(16)
        self.set_setting("api_key", key)
        return key

    def search_albums(self, query: str, limit: int = 10) -> list[dict]:
        """Live album search (Phase 4: via MetadataService — album.search
        capability; the provider chain owns the implementation and the
        plugin boundary stays provider-generic."""
        self._check("library:read")
        from services.metadata_service import MetadataService
        return MetadataService().search_album(query, limit=limit)

    def search_tracks(self, query: str, limit: int = 10) -> list[dict]:
        """Live track search (Phase 4: via MetadataService — track.search
        capability; the provider chain owns the implementation."""
        self._check("library:read")
        from services.metadata_service import MetadataService
        return MetadataService().search_track(query, limit=limit)

    def get_album_info(self, album_id: int) -> dict:
        """Album metadata (Phase 3: via MetadataService — album.metadata
        capability; used by the Lidarr plugin to build friendly release
        names)."""
        self._check("library:read")
        from services.metadata_service import MetadataService
        return MetadataService().get_album_metadata(str(album_id)) or {}

    def get_track_info(self, track_id: int) -> dict:
        """Track metadata (Phase 3: via MetadataService — track.metadata
        capability; see get_album_info)."""
        self._check("library:read")
        from services.metadata_service import MetadataService
        return MetadataService().get_track_metadata(str(track_id)) or {}

    def queue_lidarr_grab(self, item_type: str, item_id: int) -> list[int]:
        """Expand a Lidarr grab (an opaque album or track id from the
        Lidarr request) into the local library — creates Artist / Album /
        Track rows and queues one DownloadJob per track so the queue worker
        downloads them like any other track. Returns the created/queued job
        ids (moved verbatim from the former
        `services/lidarr_service.py::_create_lidarr_grab_job`)."""
        self._check("library:write")
        from services.metadata_service import MetadataService
        from models import Album, Artist, DownloadJob, Track, db

        _md = MetadataService()
        job_ids: list = []

        if item_type == "track":
            info = _md.get_track_metadata(str(item_id)) or {}
            artist_name = info.get("artist_name") or "Unknown Artist"
            track_title = info.get("title") or "Unknown Track"
            album_title = info.get("album_title") or track_title
            album_external_id = info.get("album_id")
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
            info = _md.get_album_metadata(str(item_id)) or {}
            artist_name = info.get("artist_name") or "Unknown Artist"
            album_title = info.get("title") or "Unknown Album"
            album_external_id = info.get("id") or item_id
            cover_url = info.get("cover_url")
            year = info.get("year")
            record_type = info.get("record_type") or "album"
            tracks_to_queue = _md.get_album_tracks(str(item_id))

        if not tracks_to_queue:
            log.warning("[LIDARR] Grab for %s %d returned no tracks", item_type, item_id)
            return []

        artist = Artist.query.filter_by(name=artist_name).first()
        if not artist:
            artist = Artist(
                external_id =f"lidarr:{artist_name}",
                name=artist_name,
                source="lidarr",
                monitored=True,
            )
            db.session.add(artist)
            db.session.flush()

        album = None
        if album_external_id:
            album = Album.query.filter_by(artist_id=artist.id,
                                          provider_id=None,
                                          external_id=str(album_external_id)).first()
        if not album:
            album = Album(
                artist_id=artist.id,
                name=album_title,
                year=year,
                cover_url=cover_url,
                external_id=str(album_external_id) if album_external_id else None,
                record_type=record_type,
            )
            db.session.add(album)
            db.session.flush()

        for t in tracks_to_queue:
            track = Track.query.filter_by(album_id=album.id, provider_id=None,
                                          external_id=str(t["id"])).first()
            if not track:
                track = Track(
                    album_id=album.id,
                    artist_id=artist.id,
                    title=t["title"],
                    track_number=t.get("track_position") or 0,
                    disc_number=t.get("disk_number") or 1,
                    duration=t.get("duration"),
                    external_id=str(t["id"]),
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
                album_external_id=str(album.external_id or ""),
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
        self._check("library:write")
        from models import DownloadJob, db
        j = db.session.get(DownloadJob, job_id)
        if not j:
            return False
        j.status = "cancelled"
        db.session.commit()
        return True

    def update_track_status(self, track_id: int, status: str, error_message: str = None) -> None:
        self._check("library:write")
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
        self._check("library:write")
        from models import Track, db
        t = db.session.get(Track, track_id)
        if not t:
            return
        t.caution = True
        t.caution_info = reason
        db.session.commit()

    def verify_audio_file(
        self,
        file_path: Path,
        expected_duration_seconds: Optional[float] = None,
        expected_artist: Optional[str] = None,
        expected_title: Optional[str] = None,
        max_duration_delta: float = 12.0,
        delete_on_failure: bool = True,
    ) -> tuple:
        """Generic core verification helper (Phase 2, PR 4): plugins call the
        verifier through the context instead of importing
        services.verifier_service. Enforces duration delta + embedded-tag
        match; returns (is_valid, error_reason, file_meta_dict)."""
        self._check("library:read")
        from services.verifier_service import verify_audio_file as _verify
        return _verify(
            file_path,
            expected_duration_seconds=expected_duration_seconds,
            expected_artist=expected_artist,
            expected_title=expected_title,
            max_duration_delta=max_duration_delta,
            delete_on_failure=delete_on_failure,
        )

    def verify_download_acoustid(
        self,
        file_path: str,
        expected_artist: Optional[str],
        expected_title: Optional[str],
        expected_duration: Optional[float] = None,
    ) -> dict:
        """Optional AcoustID rescue for wrong-tags-but-right-audio files
        (Phase 4): resolved through the fnack.acoustid plugin's
        fingerprint.identify capability via the manager boundary — plugins
        never import a core AcoustID implementation. Returns a dict with
        status/match info; fail-soft when AcoustID is disabled/unavailable."""
        self._check("library:read")
        try:
            from plugins.manager import plugin_manager as _pm
            from fnack.plugin_api.capabilities import FINGERPRINT_IDENTIFY
            if _pm is None or not _pm.has_capability(FINGERPRINT_IDENTIFY):
                return {"status": "unsupported"}
            for h in _pm.capability_registry.providers_for(FINGERPRINT_IDENTIFY):
                if hasattr(h.provider, "verify_download"):
                    return h.provider.verify_download(
                        file_path, expected_artist, expected_title,
                        expected_duration if expected_duration else None)
            return {"status": "unsupported"}
        except Exception:
            return {"status": "unsupported"}


# --------------------------------------------------------------------------
# Per-plugin namespaced settings (manifest-declared secrets are encrypted
# at rest — see plugins/secret_store.py)
# --------------------------------------------------------------------------

class SettingsContext:
    """Per-plugin settings, namespaced by plugin_id.

    Every access requires the manifest permission "settings". Values whose
    schema field is declared "type": "secret" are stored ENCRYPTED at rest
    (Fernet; the key lives under CONFIG_DIR, never in the DB) and decrypted
    on read — the manifest's secret declaration is honoured by the storage
    layer, not just masked in the UI.
    """

    def __init__(self, plugin_id: str, checker: PermissionChecker,
                 settings_schema: Optional[list] = None):
        self._plugin_id = plugin_id
        self._check = checker
        self._schema = {
            (f or {}).get("key"): (f or {})
            for f in (settings_schema or [])
        }

    def _is_secret_field(self, key: str) -> bool:
        field = self._schema.get(key) or {}
        return field.get("type") == "secret"

    def get(self, key: str, default=None):
        self._check("settings")
        from plugins.models import PluginSetting
        from models import db
        row = db.session.get(PluginSetting, (self._plugin_id, key))
        if row is None:
            return default
        if row.secret:
            try:
                from plugins.secret_store import decrypt
                return decrypt(row.value)
            except Exception:
                return default
        return row.value

    def set(self, key: str, value, is_secret: Optional[bool] = None) -> None:
        """Write a setting. Secret-ness is taken from the manifest
        settings_schema entry for ``key`` (type "secret") unless overridden
        by ``is_secret``."""
        self._check("settings")
        from plugins.models import PluginSetting
        from models import db
        secret = self._is_secret_field(key) if is_secret is None else bool(is_secret)
        stored = str(value)
        if secret:
            from plugins.secret_store import encrypt
            stored = encrypt(value)
        row = db.session.get(PluginSetting, (self._plugin_id, key))
        if row is None:
            row = PluginSetting(plugin_id=self._plugin_id, key=key,
                                value=stored, secret=secret)
            db.session.add(row)
        else:
            row.value = stored
            row.secret = secret
        db.session.commit()

    def all(self) -> dict:
        self._check("settings")
        from plugins.models import PluginSetting
        from plugins.secret_store import decrypt
        rows = PluginSetting.query.filter_by(plugin_id=self._plugin_id).all()
        out = {}
        for r in rows:
            if r.secret:
                try:
                    out[r.key] = decrypt(r.value)
                except Exception:
                    out[r.key] = ""
            else:
                out[r.key] = r.value
        return out


# --------------------------------------------------------------------------
# Sandboxed filesystem access
# --------------------------------------------------------------------------

class FSContext:
    """Restricts a plugin to fnack's own directories. `data_dir` is a
    private per-plugin scratch space nothing else can see."""

    def __init__(self, plugin_id: str, checker: PermissionChecker):
        import os
        self._check = checker
        self.downloads_dir = Path(os.environ.get("DOWNLOADS_DIR", "/downloads"))
        self.music_dir = Path(os.environ.get("MUSIC_DIR", "/music"))
        config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
        self.data_dir = config_dir / "plugins" / plugin_id / "data"
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def open_download_path(self, relative: str):
        self._check("filesystem:downloads")
        return self.downloads_dir / relative

    def open_music_path(self, relative: str):
        self._check("filesystem:music")
        return self.music_dir / relative

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
    """The only object a plugin ever receives.

    Every facade enforces the plugin's DECLARED manifest permissions via the
    shared PermissionChecker: network (context.http), settings, library reads
    and writes, and the sandboxed filesystem paths. A plugin that did not
    declare a permission gets a PermissionError the moment it tries to use
    the facade — the manifest is a runtime contract, not documentation.
    """

    def __init__(
        self,
        plugin_id: str,
        permissions: list[str],
        event_bus: EventBus,
        ui_slot_registry: dict[str, list],
        scheduler_hook: Callable[[float, Callable[[], None]], None],
        settings_schema: Optional[list] = None,
    ):
        checker = PermissionChecker(plugin_id, permissions)
        self._permissions = set(permissions or [])
        self.library = LibraryContext(checker)
        self.settings = SettingsContext(plugin_id, checker, settings_schema)
        self.events = EventsContext(plugin_id, event_bus)
        if "network" in self._permissions:
            self.http = requests.Session()
            self.http.headers.update({"User-Agent": f"fnack-plugin/{plugin_id}"})
        else:
            self.http = None  # plugin did not declare "network" — no outbound HTTP
        self.fs = FSContext(plugin_id, checker)
        self.ui = UIContext(plugin_id, ui_slot_registry)
        self.jobs = JobsContext(scheduler_hook)
        self.log = logging.getLogger(f"fnack.plugin.{plugin_id}")
