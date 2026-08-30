"""Denormalized per-artist counter maintenance (Phase 1, scale-to-millions).

`/api/artists` used to recompute total_albums/total_tracks/downloaded_tracks
via full GROUP BY scans on every request — the dominant cost as the library
grows. These counters live on `Artist` and are kept in sync at every write
point (see wayfinder/research/scale-to-millions.md §2.3 for the exhaustive
42-site inventory). This module is the single place the math lives so write
points call one function instead of re-implementing the queries.

IMPORTANT: counters follow `album.artist_id` (matching the original GROUP BY
in /api/artists). A track whose `artist_id` differs from its album's artist
does NOT move the artist counter — only album membership counts.
"""

from __future__ import annotations

import logging

from models import Album, Artist, Track, db

logger = logging.getLogger("fnack.counters")


def recompute_artist(artist_id: int) -> None:
    """Recompute one artist's counters from its albums/tracks (cheap, indexed)."""
    artist = db.session.get(Artist, artist_id)
    if artist is None:
        return
    total_albums = Album.query.filter_by(artist_id=artist_id).count()
    total_tracks = (
        db.session.query(Track)
        .join(Album, Track.album_id == Album.id)
        .filter(Album.artist_id == artist_id)
        .count()
    )
    downloaded_tracks = (
        db.session.query(Track)
        .join(Album, Track.album_id == Album.id)
        .filter(Album.artist_id == artist_id, Track.is_downloaded.is_(True))
        .count()
    )
    artist.total_albums = total_albums
    artist.total_tracks = total_tracks
    artist.downloaded_tracks = downloaded_tracks


def backfill_artist_counters() -> None:
    """One-time backfill for existing libraries: recompute every artist whose
    counters are still 0 (or missing). Idempotent — safe to run every boot;
    the cost is proportional to artists with no counters, not the whole library
    once populated."""
    artists = Artist.query.all()
    for artist in artists:
        if not artist.total_albums and not artist.total_tracks and not artist.downloaded_tracks:
            recompute_artist(artist.id)
    db.session.commit()
    if artists:
        logger.info("[SCALE] Backfilled artist counters for %d artists", len(artists))


# ---------------------------------------------------------------------------
# Incremental helpers for write points. Call AFTER the row change, BEFORE the
# surrounding commit (they mutate in-session; the caller commits).
# ---------------------------------------------------------------------------

def on_album_added(artist_id: int) -> None:
    artist = db.session.get(Artist, artist_id)
    if artist:
        artist.total_albums = (artist.total_albums or 0) + 1


def on_album_removed(artist_id: int) -> None:
    artist = db.session.get(Artist, artist_id)
    if artist:
        artist.total_albums = max(0, (artist.total_albums or 0) - 1)


def on_track_added(artist_id: int, downloaded: bool = False) -> None:
    artist = db.session.get(Artist, artist_id)
    if artist:
        artist.total_tracks = (artist.total_tracks or 0) + 1
        if downloaded:
            artist.downloaded_tracks = (artist.downloaded_tracks or 0) + 1


def on_track_removed(artist_id: int, downloaded: bool = False) -> None:
    artist = db.session.get(Artist, artist_id)
    if artist:
        artist.total_tracks = max(0, (artist.total_tracks or 0) - 1)
        if downloaded:
            artist.downloaded_tracks = max(0, (artist.downloaded_tracks or 0) - 1)


def on_track_downloaded(artist_id: int, is_downloaded: bool) -> None:
    """Flip the downloaded flag for a track belonging to an album of this
    artist. `is_downloaded` is the NEW value; only a False→True (or True→False)
    transition changes the counter, so callers should pass the value and this
    helper applies the delta relative to the OLD state if the caller can't
    cheaply know it — to avoid double counting, callers MUST only invoke this
    when the flag actually changed. The simple contract: +1 when becoming
    downloaded, -1 when becoming not-downloaded."""
    artist = db.session.get(Artist, artist_id)
    if not artist:
        return
    if is_downloaded:
        artist.downloaded_tracks = (artist.downloaded_tracks or 0) + 1
    else:
        artist.downloaded_tracks = max(0, (artist.downloaded_tracks or 0) - 1)
