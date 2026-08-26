"""Metadata normalization service.

Keeps the music library's tags aligned with the fnack database so Navidrome
groups albums correctly (no duplicate/split albums from mismatched ALBUM or
ALBUMARTIST tags left over by older versions or imports).

Runs automatically at container startup and periodically (via the scheduler);
can also be invoked manually. Files whose tags already match the expected
metadata are skipped, so steady-state runs are fast.
"""

import logging
import os
import shutil
from pathlib import Path

from models import Track, db
from services.queue_service import _sanitize, _tag_audio_file

logger = logging.getLogger("fnack.metadata")

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"}
MUSIC_ROOT = Path(os.environ.get("MUSIC_DIR", "/music"))


def _read_simple_tag(mf, keys):
    for k in keys:
        v = mf.get(k)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if v:
            return str(v)
    return None


def normalize_album_tags(app, quiet: bool = True) -> dict:
    """Re-tag every downloaded file with its database album/artist/title and move
    stray files into their correct album folder. Returns stats.

    Skipped when a file's ALBUM + ALBUMARTIST already match the expected values,
    so repeated runs only touch files that actually need fixing.
    """
    import mutagen

    stats = {"checked": 0, "retagged": 0, "moved": 0, "skipped": 0, "errors": 0}
    with app.app_context():
        tracks = Track.query.filter(Track.is_downloaded == True).all()  # noqa: E712
        for t in tracks:
            if not t.local_path or not os.path.isfile(t.local_path):
                stats["skipped"] += 1
                continue
            album = t.album
            if not album or not album.artist:
                stats["skipped"] += 1
                continue
            artist_name = album.artist.name
            album_name = album.name
            fp = Path(t.local_path)
            stats["checked"] += 1

            # ---- 1. Place the file in the folder its DB album belongs to ----
            expected_dir = MUSIC_ROOT / _sanitize(artist_name) / _sanitize(album_name)
            try:
                expected_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                stats["errors"] += 1
                logger.warning("[METADATA] Cannot create %s: %s", expected_dir, e)
                continue
            if fp.parent.resolve() != expected_dir.resolve():
                try:
                    new_path = expected_dir / fp.name
                    if new_path.exists() and new_path.resolve() != fp.resolve():
                        fp.unlink()
                        fp = new_path
                    else:
                        shutil.move(str(fp), str(new_path))
                        fp = new_path
                    t.local_path = str(fp)
                    t.file_path = str(fp.relative_to(MUSIC_ROOT))
                    stats["moved"] += 1
                    if not quiet:
                        logger.info("[METADATA] Moved %s -> %s", fp.name, fp)
                except OSError as e:
                    stats["errors"] += 1
                    logger.warning("[METADATA] Could not move %s: %s", fp, e)
                    continue

            # ---- 2. Re-tag only when something differs (fast steady state) ----
            try:
                mf = mutagen.File(str(fp))
                if mf is not None:
                    cur_album = _read_simple_tag(mf, ("album", "\xa9alb", "TALB"))
                    cur_albumartist = _read_simple_tag(mf, ("albumartist", "aART", "TPE2"))
                    if cur_album == album_name and cur_albumartist == artist_name:
                        stats["skipped"] += 1
                        continue
            except Exception:
                pass

            cover_bytes = None
            for cover_name in ("cover.jpg", "folder.jpg"):
                cover_path = fp.parent / cover_name
                if cover_path.is_file():
                    try:
                        cover_bytes = cover_path.read_bytes()
                    except OSError:
                        pass
                    break

            try:
                _tag_audio_file(
                    fp,
                    artist=artist_name,
                    album=album_name,
                    title=t.title,
                    track_num=t.track_number or 0,
                    year=album.year,
                    album_artist=artist_name,
                    disc_num=t.disc_number or 1,
                    total_tracks=album.tracks.count(),
                    cover_bytes=cover_bytes,
                    genre=t.genre,
                )
                stats["retagged"] += 1
                if not quiet:
                    logger.info("[METADATA] Tagged %s - %s | %s", artist_name, album_name, t.title)
            except Exception as e:
                stats["errors"] += 1
                logger.warning("[METADATA] Tagging failed for %s: %s", fp, e)

        db.session.commit()
    logger.info(
        "[METADATA] Normalize pass: %d checked, %d retagged, %d moved, %d skipped, %d errors",
        stats["checked"], stats["retagged"], stats["moved"], stats["skipped"], stats["errors"],
    )
    return stats
