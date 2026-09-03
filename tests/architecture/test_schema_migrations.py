"""Architecture test: schema migration to provider-scoped identities.

Creates a database in the OLD pre-cleanup shape (artists.spotify_id UNIQUE,
albums/tracks.deezer_id, download_jobs.album_spotify_id), runs the startup
migration (services.schema_migrations.run_schema_migrations), and verifies:

- columns renamed to provider-neutral names, data preserved,
- provider_id added + backfilled (numeric external ids -> the deezer-batch
  provider that supplied them; self-created prefixed ids stay NULL),
- the artists single-column UNIQUE on external_id is replaced by the
  per-provider (provider_id, external_id) unique (so two providers can hold
  the same external id),
- a second migration run is a no-op (idempotent).

Run from the repo root:

    .venv/bin/python tests/architecture/test_schema_migrations.py
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

OLD_ARTISTS_DDL = """
CREATE TABLE artists (
    id INTEGER PRIMARY KEY,
    spotify_id VARCHAR(64) UNIQUE NOT NULL,
    name VARCHAR(256) NOT NULL,
    image_url TEXT,
    created_at DATETIME,
    monitored BOOLEAN NOT NULL DEFAULT 1,
    auto_download BOOLEAN NOT NULL DEFAULT 0,
    last_synced_at DATETIME,
    source VARCHAR(16) NOT NULL DEFAULT 'manual',
    sync_status VARCHAR(20) NOT NULL DEFAULT 'ready',
    sync_error TEXT,
    filter_remixes BOOLEAN NOT NULL DEFAULT 1,
    filter_lofi BOOLEAN NOT NULL DEFAULT 1,
    filter_live BOOLEAN NOT NULL DEFAULT 1,
    filter_compilations BOOLEAN NOT NULL DEFAULT 1,
    include_albums BOOLEAN NOT NULL DEFAULT 1,
    include_singles BOOLEAN NOT NULL DEFAULT 1,
    include_compilations BOOLEAN NOT NULL DEFAULT 0,
    total_albums INTEGER NOT NULL DEFAULT 0,
    total_tracks INTEGER NOT NULL DEFAULT 0,
    downloaded_tracks INTEGER NOT NULL DEFAULT 0
)"""


def _build_old_db(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(f"""
    {OLD_ARTISTS_DDL};
    CREATE TABLE albums (id INTEGER PRIMARY KEY, artist_id INTEGER,
        name VARCHAR(512), year INTEGER, cover_url TEXT,
        deezer_id VARCHAR(32), record_type VARCHAR(32));
    CREATE TABLE tracks (id INTEGER PRIMARY KEY, album_id INTEGER,
        artist_id INTEGER, title VARCHAR(512), isrc VARCHAR(64),
        deezer_id VARCHAR(32), spotify_url TEXT, status VARCHAR(32),
        is_downloaded BOOLEAN NOT NULL DEFAULT 0, local_path TEXT,
        size_bytes INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE download_jobs (id INTEGER PRIMARY KEY, track_id INTEGER,
        album_id INTEGER, artist_id INTEGER, item_type VARCHAR(16),
        album_spotify_id VARCHAR(64), album_name VARCHAR(512), status VARCHAR(32));
    CREATE TABLE installed_plugins (id INTEGER PRIMARY KEY, plugin_id VARCHAR(64));
    INSERT INTO artists (id, spotify_id, name) VALUES (1, '27', 'Daft Punk');
    INSERT INTO artists (id, spotify_id, name) VALUES (2, 'acoustid:Foo', 'Foo');
    INSERT INTO albums (id, artist_id, name, deezer_id) VALUES (1, 1, 'Homework', '1001');
    INSERT INTO tracks (id, album_id, artist_id, title, deezer_id) VALUES (1, 1, 1, 'Daftendirekt', '42');
    INSERT INTO download_jobs (id, track_id, album_id, artist_id, album_spotify_id, album_name, status)
        VALUES (1, 1, 1, 1, '1001', 'Homework', 'completed');
    """)
    c.commit()
    c.close()


def test_migration_old_schema_to_provider_scoped() -> None:
    from sqlalchemy import create_engine, text

    from services.schema_migrations import run_schema_migrations

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "old.db")
        _build_old_db(db_path)
        engine = create_engine(f"sqlite:///{db_path}")
        run_schema_migrations(engine)

        con = sqlite3.connect(db_path)
        try:
            # Renames applied; old columns gone.
            artists_cols = [r[1] for r in con.execute("PRAGMA table_info(artists)")]
            assert "spotify_id" not in artists_cols and "external_id" in artists_cols
            assert "provider_id" in artists_cols
            assert "deezer_id" not in [r[1] for r in con.execute("PRAGMA table_info(albums)")]
            assert "deezer_id" not in [r[1] for r in con.execute("PRAGMA table_info(tracks)")]
            assert "album_spotify_id" not in [r[1] for r in con.execute("PRAGMA table_info(download_jobs)")]

            # Data preserved + provider backfilled (numeric -> deezer-batch).
            rows = {(r[0]): (r[1], r[2]) for r in con.execute(
                "SELECT name, provider_id, external_id FROM artists").fetchall()}
            assert rows["Daft Punk"] == ("fnack.deezer-batch", "27"), rows
            # Self-created prefixed identity stays NULL.
            assert rows["Foo"] == (None, "acoustid:Foo"), rows
            alb_prov = con.execute(
                "SELECT provider_id FROM albums WHERE id=1").fetchone()
            assert alb_prov == ("fnack.deezer-batch",), alb_prov

            # The artists UNIQUE on external_id is gone (origin 'u' absent),
            # and the per-provider unique index exists.
            idx = [r for r in con.execute("PRAGMA index_list('artists')").fetchall()]
            assert not any(str(r[3]) == "u" for r in idx), idx
            assert any("uq_artists_provider_external" in str(r[1]) for r in idx), idx

            # Two providers may now hold the same external id.
            con.execute(
                "INSERT INTO artists (provider_id, external_id, name) "
                "VALUES ('com.example.other', '27', 'Other')")
            con.commit()
            assert con.execute(
                "SELECT COUNT(*) FROM artists WHERE external_id='27'").fetchone()[0] == 2
        finally:
            con.close()

        # Idempotent: a second run changes nothing and does not error.
        run_schema_migrations(engine)
        con = sqlite3.connect(db_path)
        artists_cols = [r[1] for r in con.execute("PRAGMA table_info(artists)")]
        assert "spotify_id" not in artists_cols
        con.close()


if __name__ == "__main__":
    test_migration_old_schema_to_provider_scoped()
    print("test_schema_migrations: PASSED")
