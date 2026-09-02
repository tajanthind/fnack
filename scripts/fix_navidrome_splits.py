#!/usr/bin/env python3
"""One-time repair for Navidrome album splits.

Background
----------
Navidrome (0.63.x) identifies an album by (album artist, album name,
release date). fnack used to leave per-track `originaldate`/`releasedate`
tags from the source downloader in the files, so every song in an album got
its own release date and Navidrome split one album into many rows (one per
batch of songs scanned together). fnack v0.2.24+ strips those tags and
re-tags every file with a uniform date, but the split album rows already in
Navidrome's database do not merge by themselves on a rescan.

This script merges those rows: for every (album artist, album name) group it
keeps the row with the most songs, repoints all media files to it, merges
cover-art links, and deletes the leftover rows. Favorites/playlists are kept
(they reference media files, whose IDs do not change).

Usage
-----
Stop Navidrome, then:

    python3 scripts/fix_navidrome_splits.py /path/to/navidrome.db

or run it on a copy and swap it in. Afterwards start Navidrome and trigger a
full library rescan (Settings -> Rescan) so it re-verifies everything.

Note: fnack also runs this automatically at every restart when the
"navidrome_db_path" setting is configured (Settings -> Navidrome), so the
manual run is only needed for an immediate one-off fix.
"""

import sys


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    # Import the shared implementation (works without a Flask app context).
    sys.path.insert(0, __file__.rsplit("/", 2)[0])
    sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/bundled_plugins/fnack.navidrome")
    import navidrome as _navidrome

    stats = _navidrome.consolidate_split_albums(sys.argv[1])
    if stats["groups"] == 0:
        print("No split album rows found — nothing to do.")
        return 0
    print(
        f"Done: merged {stats['merged_rows']} split album row(s) across "
        f"{stats['groups']} group(s), repointed {stats['moved_files']} media file(s)."
    )
    print("Start Navidrome and trigger a full rescan to re-verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
