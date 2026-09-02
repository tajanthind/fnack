#!/usr/bin/env python3
"""fnack library re-verification tool.

Checks every downloaded track against the OFFICIAL duration (fetched from the
Deezer public API, no authentication) and marks confirmed mismatches as failed
so they can be inspected or re-downloaded. Files are NOT deleted.

Also repairs the stored track duration with the official value (an earlier bug
overwrote the expected duration with the downloaded file's actual duration,
which masked mismatches).

Usage (inside the container, against the mounted config DB):
    docker exec fnack python3 /app/scripts/reverify_library.py
"""

import os
import sys
import time
from pathlib import Path

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root_dir)
sys.path.insert(0, os.path.join(root_dir, "services"))

from flask import Flask  # noqa: E402
from models import Album, AppSetting, Track, db  # noqa: E402
from verifier_service import DEFAULT_DURATION_DELTA_SECONDS  # noqa: E402

DB_URI = os.environ.get("SQLALCHEMY_DATABASE_URI", "sqlite:////config/fnack.db")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

STRICTNESS_DELTAS = {"strict": 4.0, "standard": 8.0, "lenient": 15.0}


def main() -> int:
    import mutagen  # local import: keep startup fast

    with app.app_context():
        strictness = "standard"
        setting = db.session.get(AppSetting, "matching_strictness")
        if setting:
            strictness = setting.value or "standard"
        max_delta = STRICTNESS_DELTAS.get(strictness, DEFAULT_DURATION_DELTA_SECONDS)

        checked = 0
        flagged = 0
        skipped = 0
        repaired_durations = 0

        tracks = Track.query.filter(Track.is_downloaded == True).all()  # noqa: E712
        for idx, t in enumerate(tracks):
            if not t.local_path or not os.path.isfile(t.local_path):
                skipped += 1
                continue

            # Official expected duration: Deezer first, else the stored value
            official = None
            if t.deezer_id and str(t.deezer_id).isdigit():
                try:
                    from services.metadata_service import MetadataService
                    info = MetadataService().get_track_metadata(str(t.deezer_id)) or {}
                    official = info.get("duration")
                except Exception:
                    official = None
                time.sleep(0.1)  # polite pacing toward the public API

            expected = official or t.duration
            if not expected or expected <= 0:
                skipped += 1
                continue

            # Repair the stored expected duration with the official one
            if official and t.duration != official:
                t.duration = official
                repaired_durations += 1

            try:
                mf = mutagen.File(t.local_path)
                if mf is None or mf.info is None:
                    skipped += 1
                    continue
                actual = getattr(mf.info, "length", None)
            except Exception:
                skipped += 1
                continue

            checked += 1
            if actual is None:
                skipped += 1
                continue

            delta = abs(actual - expected)
            if delta > max_delta:
                flagged += 1
                t.is_downloaded = False
                t.status = "failed"
                t.error_message = (
                    f"Re-verification failed: file duration {actual:.1f}s vs official "
                    f"{expected:.1f}s (delta {delta:.1f}s > {max_delta:.1f}s tolerance). "
                    "File kept on disk; delete or re-download to fix."
                )
                # Phase 1 (scale-to-millions): track flipped to missing → −1 downloaded.
                try:
                    from services.counters_service import on_track_downloaded
                    if t.album_id and t.album and t.album.artist_id:
                        on_track_downloaded(t.album.artist_id, is_downloaded=False)
                except Exception:
                    pass
                print(
                    f"[MISMATCH] track {t.id} '{t.title[:45]}' -> {t.local_path} "
                    f"(got {actual:.1f}s, official {expected:.1f}s)"
                )
                album = t.album
                if album:
                    album_tracks = album.tracks.all()
                    album.is_downloaded = all(x.is_downloaded for x in album_tracks) and len(album_tracks) > 0
                    album.size_bytes = sum(x.size_bytes or 0 for x in album_tracks)

            if idx % 25 == 0:
                db.session.commit()  # checkpoint progress

        db.session.commit()
        print(f"\nChecked {checked} downloaded tracks | {flagged} mismatched | {skipped} skipped")
        if repaired_durations:
            print(f"Repaired {repaired_durations} stored durations with official values")
        print("Mismatched files were left on disk and their tracks marked failed for re-download.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
