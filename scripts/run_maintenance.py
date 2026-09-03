#!/usr/bin/env python3
"""fnack full library maintenance (run as a detached subprocess).

Does the heavy library work that used to run inline in the web process and
slowed the dashboard down:
  1. normalize_album_tags   - merge duplicate albums (same-artist, edition,
                              fuzzy, cross-artist/collab), re-tag files to the
                              canonical album/artist, strip per-track date tags,
                              backfill missing album artwork, clean empty dirs.

It is spawned at container boot and periodically (6h) by app.py. A lock file
guarantees only one maintenance run at a time, even if boot and the periodic
scheduler overlap. Runs entirely out-of-process, so the web UI stays
responsive no matter how big the library is.

Manual run:
    docker exec fnack python3 /app/scripts/run_maintenance.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # project root
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/services")

from flask import Flask  # noqa: E402
from models import db  # noqa: E402
from services.tag_normalization_service import MUSIC_ROOT, normalize_album_tags  # noqa: E402

DB_URI = os.environ.get("SQLALCHEMY_DATABASE_URI", "sqlite:////config/fnack.db")
LOCK_FILE = os.environ.get("MAINTENANCE_LOCK", "/downloads/work/maintenance.lock")


def _acquire_lock() -> bool:
    """Try to take an exclusive lock; False when another run is active."""
    try:
        import fcntl
    except ImportError:
        return True  # no fcntl (non-Linux) — just run
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        # keep fd alive for the whole process (lock released on exit)
        globals()["_lock_fd"] = fd
        return True
    except OSError:
        return True


def main() -> int:
    if not _acquire_lock():
        print("[MAINTENANCE] Another maintenance run is already active; skipping.")
        return 0

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    if os.environ.get("MUSIC_DIR"):
        MUSIC_ROOT._path = os.environ["MUSIC_DIR"]

    t0 = time.time()
    print("[MAINTENANCE] Starting library maintenance...")
    stats = normalize_album_tags(app, quiet=False)
    print(
        f"[MAINTENANCE] Done in {time.time() - t0:.1f}s | "
        f"checked {stats.get('checked', 0)}, retagged {stats.get('retagged', 0)}, "
        f"moved {stats.get('moved', 0)}, merged {stats.get('merged_albums', 0)} album(s), "
        f"{stats.get('removed_dup_tracks', 0)} dup track(s) removed, "
        f"{stats.get('covers_backfilled', 0)} cover(s) saved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
