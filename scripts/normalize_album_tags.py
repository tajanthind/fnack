#!/usr/bin/env python3
"""fnack library tag normalization tool (manual run).

Fixes albums that Navidrome splits into multiple entries with the same name by
re-tagging every downloaded file with its database album/artist/title and
moving stray files into their correct album folder.

This also runs automatically at container startup and periodically; run it
manually here when you want an immediate pass.

Usage (inside the container, against the mounted config DB):
    docker exec fnack python3 /app/scripts/normalize_album_tags.py
"""

import os
import sys

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root_dir)
sys.path.insert(0, os.path.join(root_dir, "services"))

from flask import Flask  # noqa: E402
from models import db  # noqa: E402
from services.tag_normalization_service import MUSIC_ROOT, normalize_album_tags  # noqa: E402

DB_URI = os.environ.get("SQLALCHEMY_DATABASE_URI", "sqlite:////config/fnack.db")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

# MUSIC_ROOT defaults to /music; allow override for testing
if os.environ.get("MUSIC_DIR"):
    MUSIC_ROOT._path = os.environ["MUSIC_DIR"]


def main() -> int:
    stats = normalize_album_tags(app, quiet=False)
    print(
        f"\nChecked {stats['checked']} files | retagged {stats['retagged']} | "
        f"moved {stats['moved']} | skipped {stats['skipped']} | errors {stats['errors']}"
    )
    print("Run a Navidrome scan afterwards so it re-groups the albums.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
