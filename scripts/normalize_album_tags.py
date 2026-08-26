#!/usr/bin/env python3
"""fnack library tag normalization tool.

Fixes albums that Navidrome splits into multiple entries with the same name.
Causes: older fnack versions / imports left files with mismatched ALBUM tags
(e.g. single-track files inside an album folder tagged as the single's name),
so Navidrome groups them into separate albums.

This re-tags every downloaded file with the metadata from the fnack database
(album = the DB album, album artist = the artist), matching how Navidrome
groups albums. Embedded covers are preserved.

Usage (inside the container, against the mounted config DB):
    docker exec fnack python3 /app/scripts/normalize_album_tags.py
"""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/services")

from flask import Flask  # noqa: E402
from models import Album, Track, db  # noqa: E402
from queue_service import _sanitize, _tag_audio_file  # noqa: E402

DB_URI = os.environ.get("SQLALCHEMY_DATABASE_URI", "sqlite:////config/fnack.db")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"}


def main() -> int:
    with app.app_context():
        music_root = Path("/music")
        tracks = Track.query.filter(Track.is_downloaded == True).all()  # noqa: E712
        fixed = 0
        moved = 0
        skipped = 0
        for t in tracks:
            if not t.local_path or not os.path.isfile(t.local_path):
                skipped += 1
                continue
            album = t.album
            if not album or not album.artist:
                skipped += 1
                continue
            artist_name = album.artist.name
            album_name = album.name
            fp = Path(t.local_path)

            # ---- 1. Place the file in the folder its DB album belongs to ----
            expected_dir = music_root / _sanitize(artist_name) / _sanitize(album_name)
            expected_dir.mkdir(parents=True, exist_ok=True)
            if fp.parent.resolve() != expected_dir.resolve():
                try:
                    new_path = expected_dir / fp.name
                    if new_path.exists() and new_path.resolve() != fp.resolve():
                        # Duplicate at the destination: keep the DB copy and remove the stray
                        fp.unlink()
                        fp = new_path
                    else:
                        shutil.move(str(fp), str(new_path))
                        fp = new_path
                    t.local_path = str(fp)
                    t.file_path = str(fp.relative_to(music_root))
                    moved += 1
                    print(f"[MOVED] {fp.name} -> {fp}")
                except OSError as e:
                    print(f"[WARN] Could not move {fp}: {e}")

            # ---- 2. Re-tag with the DB metadata ----
            cover_bytes = None
            for cover_name in ("cover.jpg", "folder.jpg"):
                cover_path = fp.parent / cover_name
                if cover_path.is_file():
                    try:
                        cover_bytes = cover_path.read_bytes()
                    except OSError:
                        pass
                    break

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
            fixed += 1

        db.session.commit()
        print(f"\nRe-tagged {fixed} files, moved {moved} to their correct album folder, skipped {skipped}.")
        print("Run a Navidrome scan afterwards so it re-groups the albums.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
