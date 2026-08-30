"""Folder Watcher Service for fnack: Monitors /music directory in real-time.

Detects when audio files are deleted, renamed, or moved, and automatically
synchronizes track library states and Navidrome metadata.
"""

import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Optional
from watchdog.events import FileSystemEventHandler, FileSystemMovedEvent
from watchdog.observers import Observer

from models import Album, AppSetting, Track, db

logger = logging.getLogger("fnack.watcher")
AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"}
IGNORE_PATTERNS = {".tmp", ".part", ".crdownload", ".ytdl", ".DS_Store", ".fnack_tmp"}

_observer: Optional[Observer] = None
_current_watched_path: Optional[str] = None


def _normalize(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    return re.sub(r"[^a-zA-Z0-9]+", "", s).lower()


def _maintenance_running() -> bool:
    """True while the detached library-maintenance subprocess is active.

    Maintenance intentionally deletes/moves files (dedupe, album merges) —
    the watcher must not race it by marking tracks missing for those changes,
    otherwise the two fight over the same rows (0-rows-matched errors) and
    flood the log. Uses the same flock the maintenance process holds.
    """
    import fcntl

    lock = os.environ.get("MAINTENANCE_LOCK", "/downloads/work/maintenance.lock")
    try:
        if not os.path.exists(lock):
            return False
        fd = os.open(lock, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # locked by the maintenance process
        finally:
            os.close(fd)
        return False
    except Exception:
        return False


class MusicFolderHandler(FileSystemEventHandler):
    def __init__(self, app, socketio, music_root: Path):
        super().__init__()
        self.app = app
        self.socketio = socketio
        self.music_root = music_root

    def _is_audio_file(self, path_str: str) -> bool:
        p = Path(path_str)
        if any(ign in p.name for ign in IGNORE_PATTERNS):
            return False
        return p.suffix.lower() in AUDIO_EXTENSIONS

    def _find_replacement(self, deleted_path: Path, track) -> Optional[Path]:
        """Return a file that replaced the deleted one for the same track slot.

        fnack's own download pipeline replaces files in place (e.g. an .opus
        upgraded to a lossless .flac, or a re-download with a better source).
        The deletion of the old file must not be interpreted as the user
        removing the track: if a new audio file for the same track number or
        title now exists in the same directory, treat it as a replacement.
        """
        parent = deleted_path.parent
        if not parent.is_dir():
            return None

        # In-place replacement: the file already exists again at the deleted path
        # (re-download / re-sync writing over the same filename). Treat as replacement.
        if deleted_path.exists() and deleted_path.stat().st_size > 0:
            return deleted_path

        track_num = track.track_number
        disc_num = track.disc_number or 1
        disc_prefix = f"{disc_num}-" if disc_num > 1 else ""
        norm_title = _normalize(track.title)

        for f in parent.iterdir():
            if not f.is_file() or not self._is_audio_file(str(f)):
                continue
            if f.resolve() == deleted_path.resolve():
                continue
            name = f.name
            if track_num and (
                name.startswith(f"{track_num:02d}. ")
                or name.startswith(f"{disc_prefix}{track_num:02d}. ")
            ):
                return f
            if norm_title and norm_title in _normalize(f.stem):
                return f
        return None

    def on_deleted(self, event):
        if event.is_directory or not self._is_audio_file(event.src_path):
            return
        if _maintenance_running():
            logger.debug("[WATCHER] Skipping deletion event (maintenance running): %s", event.src_path)
            return

        deleted_path = str(Path(event.src_path).resolve())
        logger.info("[WATCHER] Audio file deleted: %s", deleted_path)

        with self.app.app_context():
            try:
                # Find track matching local_path
                track = Track.query.filter(
                    (Track.local_path == deleted_path) | (Track.local_path == event.src_path)
                ).first()

                if not track:
                    # Try matching by relative path
                    try:
                        rel_path = str(Path(event.src_path).relative_to(self.music_root))
                        track = Track.query.filter(Track.file_path == rel_path).first()
                    except Exception:
                        pass

                if track:
                    # If the deletion was actually a file replacement by fnack's own
                    # pipeline (format upgrade / re-download), re-point the track at the
                    # new file instead of marking it missing.
                    replacement = self._find_replacement(Path(deleted_path), track)
                    if replacement:
                        track.local_path = str(replacement)
                        try:
                            track.file_path = str(replacement.relative_to(self.music_root))
                        except Exception:
                            pass
                        track.file_format = replacement.suffix.lower().lstrip(".")
                        track.status = "completed"
                        track.is_downloaded = True
                        track.size_bytes = replacement.stat().st_size
                        db.session.commit()
                        logger.info("[WATCHER] File %s replaced by %s; track %d kept downloaded", deleted_path, replacement.name, track.id)
                        return

                    logger.info("[WATCHER] Marking track %d ('%s') as missing due to file deletion", track.id, track.title)
                    track.is_downloaded = False
                    track.status = "missing"
                    track.local_path = None
                    track.file_path = ""
                    track.size_bytes = 0
                    track.progress = 0.0

                    # Phase 1 (scale-to-millions): track flipped to missing → −1 downloaded.
                    try:
                        from services.counters_service import on_track_downloaded
                        if track.album_id and track.album and track.album.artist_id:
                            on_track_downloaded(track.album.artist_id, is_downloaded=False)
                    except Exception:
                        logger.debug("[SCALE] counter update skipped", exc_info=True)

                    album = track.album
                    if album:
                        tracks = album.tracks.all()
                        album.is_downloaded = all(t.is_downloaded for t in tracks if t.id != track.id) and len(tracks) > 0
                        album.size_bytes = sum(t.size_bytes or 0 for t in tracks if t.id != track.id)

                    db.session.commit()

                    self.socketio.emit("download_progress", {
                        "track_id": track.id,
                        "album_id": track.album_id,
                        "artist_id": track.artist_id,
                        "status": "missing",
                        "progress": 0.0,
                    })
                    self.socketio.emit("artist_updated", {"artist_id": track.artist_id})
                    self.socketio.emit("toast", {
                        "message": f"File deleted: Track '{track.title}' marked as missing",
                        "type": "info",
                    })
            except Exception as e:
                # A failed commit leaves the session in a broken state; without
                # a rollback every later event in this greenlet also fails.
                try:
                    db.session.rollback()
                except Exception:
                    pass
                logger.warning("[WATCHER] Error handling deleted file %s: %s", deleted_path, e)

    def on_moved(self, event):
        if event.is_directory or not (self._is_audio_file(event.src_path) or self._is_audio_file(event.dest_path)):
            return
        if _maintenance_running():
            logger.debug("[WATCHER] Skipping move event (maintenance running): %s", event.src_path)
            return

        src_path = str(Path(event.src_path).resolve())
        dest_path = str(Path(event.dest_path).resolve())
        logger.info("[WATCHER] Audio file moved: %s -> %s", src_path, dest_path)

        with self.app.app_context():
            try:
                track = Track.query.filter(
                    (Track.local_path == src_path) | (Track.local_path == event.src_path)
                ).first()

                if track:
                    track.local_path = dest_path
                    try:
                        track.file_path = str(Path(dest_path).relative_to(self.music_root))
                    except Exception:
                        pass
                    track.file_format = Path(dest_path).suffix.lower().lstrip(".")
                    db.session.commit()
                    logger.info("[WATCHER] Updated file location for track %d ('%s')", track.id, track.title)
                    self.socketio.emit("artist_updated", {"artist_id": track.artist_id})
            except Exception as e:
                logger.warning("[WATCHER] Error handling moved file: %s", e)


def start_folder_watcher(app, socketio):
    """Start watchdog observer on configured music directory."""
    global _observer, _current_watched_path

    with app.app_context():
        s = db.session.get(AppSetting, "music_path")
        music_path_str = s.value if s else "/music"
        s_enabled = db.session.get(AppSetting, "enable_folder_watcher")
        enabled = s_enabled.value.lower() != "false" if s_enabled else True

    if not enabled:
        logger.info("[WATCHER] Folder watcher is disabled in settings")
        return

    music_path = Path(music_path_str)
    if not music_path.exists():
        try:
            music_path.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Fallback to repository local music directory if running outside docker
            fallback_dir = Path(__file__).resolve().parent.parent / "music"
            if fallback_dir.exists():
                music_path = fallback_dir
            else:
                logger.warning("[WATCHER] Cannot create or access music directory %s", music_path)
                return

    if _observer and _observer.is_alive():
        if _current_watched_path == str(music_path):
            return
        logger.info("[WATCHER] Restarting observer for new path: %s", music_path)
        _observer.stop()
        _observer.join(timeout=3)

    handler = MusicFolderHandler(app, socketio, music_path)
    _observer = Observer()
    _observer.schedule(handler, str(music_path), recursive=True)
    _observer.daemon = True
    _observer.start()
    _current_watched_path = str(music_path)
    logger.info("[WATCHER] Watching music folder: %s", music_path)
