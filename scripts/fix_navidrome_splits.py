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

Only the `album` and `media_file` tables are modified; if the schema ever
changes this script refuses to run rather than corrupt anything.
"""

import sqlite3
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    db_path = sys.argv[1]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ---- schema guards ----------------------------------------------------
    album_cols = {r[1] for r in cur.execute("PRAGMA table_info(album)")}
    file_cols = {r[1] for r in cur.execute("PRAGMA table_info(media_file)")}
    for need in ("id", "album_artist", "name", "song_count", "embed_art_path",
                 "small_image_url", "large_image_url", "release_date"):
        if need not in album_cols:
            print(f"ERROR: unexpected Navidrome schema (album has no '{need}'). Refusing to touch it.")
            return 1
    for need in ("album_id", "missing"):
        if need not in file_cols:
            print(f"ERROR: unexpected Navidrome schema (media_file has no '{need}'). Refusing to touch it.")
            return 1

    groups = cur.execute(
        "SELECT album_artist, name, COUNT(*) c FROM album "
        "GROUP BY album_artist, name HAVING COUNT(*) > 1"
    ).fetchall()
    if not groups:
        print("No split album rows found — nothing to do.")
        return 0

    print(f"Found {len(groups)} split album group(s); consolidating...")
    merged_rows = 0
    moved_files = 0

    for g in groups:
        rows = cur.execute(
            "SELECT id, embed_art_path, small_image_url, large_image_url, release_date, date "
            "FROM album WHERE album_artist=? AND name=? ORDER BY song_count DESC",
            (g["album_artist"], g["name"]),
        ).fetchall()
        if len(rows) < 2:
            continue
        canon = rows[0]
        others = [r for r in rows[1:]]
        other_ids = [r["id"] for r in others]
        placeholders = ",".join("?" * len(other_ids))

        # Repoint every media file to the canonical album row
        cur.execute(
            f"UPDATE media_file SET album_id=? WHERE album_id IN ({placeholders})",
            [canon["id"]] + other_ids,
        )
        moved_files += cur.rowcount

        # Fill the canonical row's art/date from the richest leftover
        for o in others:
            if not canon["embed_art_path"] and o["embed_art_path"]:
                cur.execute("UPDATE album SET embed_art_path=? WHERE id=?", (o["embed_art_path"], canon["id"]))
            for col in ("small_image_url", "large_image_url"):
                if not canon[col] and o[col]:
                    cur.execute(f"UPDATE album SET {col}=? WHERE id=?", (o[col], canon["id"]))
            if not canon["release_date"] and o["release_date"]:
                cur.execute("UPDATE album SET release_date=? WHERE id=?", (o["release_date"], canon["id"]))
            if not canon["date"] and o["date"]:
                cur.execute("UPDATE album SET date=? WHERE id=?", (o["date"], canon["id"]))

        cur.execute(f"DELETE FROM album WHERE id IN ({placeholders})", other_ids)
        merged_rows += len(others)

        # Refresh the visible song count from the actual files
        n = cur.execute(
            "SELECT COUNT(*) c FROM media_file WHERE album_id=? AND missing=0", (canon["id"],)
        ).fetchone()["c"]
        cur.execute("UPDATE album SET song_count=? WHERE id=?", (n, canon["id"]))

    # Rebuild the album full-text index so search stays consistent
    try:
        cur.execute("INSERT INTO album_fts(album_fts) VALUES('rebuild')")
    except Exception:
        pass  # some schemas rebuild FTS on the next scan

    conn.commit()
    conn.close()
    print(f"Done: merged {merged_rows} split album row(s), repointed {moved_files} media file(s).")
    print("Start Navidrome and trigger a full rescan to re-verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
