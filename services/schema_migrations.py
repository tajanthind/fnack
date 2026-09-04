"""Idempotent SQLite schema migrations for existing fnack databases.

Fresh databases get the full schema from ``db.create_all()`` (the models);
this module only upgrades databases created by OLDER versions: additive
columns, provider-neutral identity renames, and the provider-scoped identity
columns/backfill/unique rebuild. Runs at startup, before any ORM query
touches the new columns.

All DDL runs in AUTOCOMMIT so ``PRAGMA foreign_keys`` toggling and the
artists table rebuild behave correctly (neither works inside a transaction).
Every step is guarded: a step that already applied (or is irrelevant to this
database's age) is a no-op.
"""

from __future__ import annotations

import logging

from models import db

logger = logging.getLogger("fnack.schema")

# One-time backfill note: every artist in an existing library with a bare
# numeric external id was supplied by the fnack.deezer-batch provider (the
# only provider that has ever produced numeric artist ids); albums/tracks
# inherit their artist's provider. Self-created identities (acoustid:/lidarr:
# prefixed) stay NULL. This is migration-only data provenance — core never
# interprets provider_id or external_id at runtime.


def run_schema_migrations(engine=None) -> None:
    """Run all idempotent schema migrations against the app engine."""
    engine = engine or db.engine
    try:
        with engine.connect() as conn:
            # AUTOCOMMIT (see module docstring).
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")

            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_artists_name ON artists (name)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_albums_artist_id ON albums (artist_id)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_tracks_album_id ON tracks (album_id)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_tracks_artist_id ON tracks (artist_id)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_tracks_is_downloaded ON tracks (is_downloaded)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks (status)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks (isrc)"))
            conn.execute(db.text("CREATE INDEX IF NOT EXISTS idx_jobs_status ON download_jobs (status)"))

            # Safe column additions for existing databases
            try:
                conn.execute(db.text("ALTER TABLE albums ADD COLUMN monitored BOOLEAN DEFAULT 1"))
            except Exception:
                pass
            try:
                conn.execute(db.text("ALTER TABLE tracks ADD COLUMN monitored BOOLEAN DEFAULT 1"))
            except Exception:
                pass
            try:
                conn.execute(db.text("ALTER TABLE tracks ADD COLUMN genre VARCHAR(128)"))
            except Exception:
                pass

            # v0.2.34+: MusicBrainz enrichment columns (albums)
            for col, ddl in [
                ("mb_release_group_id", "VARCHAR(64)"),
                ("mb_title", "VARCHAR(512)"),
                ("mb_year", "INTEGER"),
                ("mb_checked_at", "DATETIME"),
            ]:
                try:
                    conn.execute(db.text(f"ALTER TABLE albums ADD COLUMN {col} {ddl}"))
                except Exception:
                    pass
            # v0.2.34+: AcoustID caution flag (tracks)
            try:
                conn.execute(db.text("ALTER TABLE tracks ADD COLUMN caution BOOLEAN DEFAULT 0"))
            except Exception:
                pass
            try:
                conn.execute(db.text("ALTER TABLE tracks ADD COLUMN caution_info TEXT"))
            except Exception:
                pass
            # Phase 1: user-facing priority override (plugins)
            try:
                conn.execute(db.text("ALTER TABLE installed_plugins ADD COLUMN priority_override INTEGER"))
            except Exception:
                pass
            # Plugin secrets at rest: mark which PluginSetting rows are
            # manifest-declared secrets (encrypted values).
            try:
                conn.execute(db.text("ALTER TABLE plugin_settings ADD COLUMN secret BOOLEAN DEFAULT 0"))
            except Exception:
                pass
            # Phase 1: denormalized per-artist counters (scale-to-millions)
            for col in ("total_albums", "total_tracks", "downloaded_tracks"):
                try:
                    conn.execute(db.text(f"ALTER TABLE artists ADD COLUMN {col} INTEGER DEFAULT 0"))
                except Exception:
                    pass

            # Provider-neutral identity columns (final cleanup): the external
            # identity columns are no longer named after a provider. Fresh
            # databases already have the new names (create_all); older
            # databases are renamed in place (SQLite RENAME COLUMN preserves
            # data, NOT NULL/unique constraints, and index definitions). The
            # values are opaque provider identities — nothing here interprets
            # them.
            for table, old_col, new_col in [
                ("artists", "spotify_id", "external_id"),
                ("albums", "deezer_id", "external_id"),
                ("tracks", "deezer_id", "external_id"),
                ("download_jobs", "album_spotify_id", "album_external_id"),
            ]:
                try:
                    conn.execute(db.text(
                        f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}"))
                except Exception:
                    pass

            # Provider-scoped identities (provider-neutral core cleanup):
            # record WHICH provider supplied each external id so different
            # providers can use the same id for different entities. provider_id
            # stores the plugin id of the supplying provider (data, never
            # interpreted by core); NULL means the identity is self-created.
            for table in ("artists", "albums", "tracks"):
                try:
                    conn.execute(db.text(
                        f"ALTER TABLE {table} ADD COLUMN provider_id VARCHAR(64)"))
                except Exception:
                    pass
            try:
                conn.execute(db.text(
                    "UPDATE artists SET provider_id = 'fnack.deezer-batch' "
                    "WHERE provider_id IS NULL AND external_id GLOB '[0-9]*'"))
                conn.execute(db.text(
                    "UPDATE albums SET provider_id = "
                    "(SELECT a.provider_id FROM artists a WHERE a.id = albums.artist_id) "
                    "WHERE provider_id IS NULL"))
                conn.execute(db.text(
                    "UPDATE tracks SET provider_id = "
                    "(SELECT a.provider_id FROM artists a WHERE a.id = tracks.artist_id) "
                    "WHERE provider_id IS NULL"))
            except Exception:
                pass

            # Artist identity uniqueness is now per-provider: drop the old
            # single-column UNIQUE on artists.external_id (a SQLite autoindex,
            # which requires a table rebuild) so two different providers may
            # hold the same external id for different artists. The composite
            # unique (provider_id, external_id) is recreated below; fresh
            # databases get it from create_all.
            try:
                idx_rows = conn.execute(db.text("PRAGMA index_list('artists')")).fetchall()
                old_unique = any(str(r[3]) == "u" for r in idx_rows)  # origin 'u' = column UNIQUE
                if old_unique:
                    logger.info("[SCHEMA] Rebuilding artists table for per-provider identity uniqueness")
                    conn.execute(db.text("PRAGMA foreign_keys=OFF"))
                    conn.execute(db.text("""
                        CREATE TABLE artists_provider_id (
                            id INTEGER PRIMARY KEY,
                            provider_id VARCHAR(64),
                            external_id VARCHAR(64) NOT NULL,
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
                        )
                    """))
                    cols = ("id, provider_id, external_id, name, image_url, created_at, "
                            "monitored, auto_download, last_synced_at, source, sync_status, "
                            "sync_error, filter_remixes, filter_lofi, filter_live, "
                            "filter_compilations, include_albums, include_singles, "
                            "include_compilations, total_albums, total_tracks, downloaded_tracks")
                    conn.execute(db.text(
                        f"INSERT INTO artists_provider_id ({cols}) SELECT {cols} FROM artists"))
                    conn.execute(db.text("DROP TABLE artists"))
                    conn.execute(db.text("ALTER TABLE artists_provider_id RENAME TO artists"))
                    conn.execute(db.text("PRAGMA foreign_keys=ON"))
            except Exception:
                logger.exception("[SCHEMA] artists per-provider rebuild failed")

            # Indexes for the provider-scoped identity model (recreated after
            # the artists rebuild dropped the old ones; idempotent).
            for ddl in [
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON download_jobs (status, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_updated ON download_jobs (status, updated_at)",
                "CREATE INDEX IF NOT EXISTS idx_artists_name ON artists (name)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_artists_provider_external ON artists (provider_id, external_id)",
                "CREATE INDEX IF NOT EXISTS ix_artists_provider_id ON artists (provider_id)",
                "CREATE INDEX IF NOT EXISTS ix_artists_monitored ON artists (monitored)",
                "CREATE INDEX IF NOT EXISTS ix_artists_sync_status ON artists (sync_status)",
                "CREATE INDEX IF NOT EXISTS ix_albums_provider_id ON albums (provider_id)",
                "CREATE INDEX IF NOT EXISTS ix_tracks_provider_id ON tracks (provider_id)",
            ]:
                try:
                    conn.execute(db.text(ddl))
                except Exception:
                    pass
            conn.commit()
    except Exception:
        logger.exception("[SCHEMA] schema migrations failed")
