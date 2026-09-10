"""Provider-neutral library queries for integrations.

A server/client integration needs to search and page through fnack's library
without loading it into memory and without knowing anything about metadata
providers. This module is that interface: SQL-side filtering, ordering and
pagination (``LIMIT``/``OFFSET`` + ``COUNT``), stable internal ids in the
results, and provider identities exposed only as opaque, separately scoped
values.

Rules:

* **Provider-neutral.** No provider implementation is imported or named;
  ``provider_id``/``external_id`` are returned verbatim because core never
  interprets them.
* **Stable ids.** Every result carries the internal ``Artist``/``Album``/
  ``Track`` id — the reference user state and integrations should store.
* **Bounded work.** Callers get a page + ``total``; ordering keys are
  whitelisted so a caller cannot inject arbitrary SQL.
* Reads only: this module never mutates the library.
"""

from __future__ import annotations

from sqlalchemy import or_

from models import Album, Artist, Track, db

DEFAULT_PAGE = 100
MAX_PAGE = 500

_ARTIST_SORTS = {"name": Artist.name, "created": Artist.created_at, "id": Artist.id}
_ALBUM_SORTS = {"name": Album.name, "year": Album.year, "created": Album.created_at,
                "id": Album.id}
_TRACK_SORTS = {"title": Track.title, "duration": Track.duration,
                "track_number": Track.track_number, "created": Track.created_at,
                "id": Track.id}


class LibraryQueryError(ValueError):
    """Invalid query argument (bad sort key, bad page size, bad id)."""


def _page(offset, limit) -> tuple[int, int]:
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        raise LibraryQueryError("offset must be an integer")
    try:
        limit = int(limit) if limit is not None else DEFAULT_PAGE
    except (TypeError, ValueError):
        raise LibraryQueryError("limit must be an integer")
    if limit < 1:
        raise LibraryQueryError("limit must be >= 1")
    return offset, min(limit, MAX_PAGE)


def _sort_for(sorts: dict, sort, order):
    if sort not in sorts:
        raise LibraryQueryError(
            f"unknown sort {sort!r} (expected one of {', '.join(sorted(sorts))})")
    direction = (order or "asc").lower()
    if direction not in ("asc", "desc"):
        raise LibraryQueryError("order must be 'asc' or 'desc'")
    column = sorts[sort]
    return column.desc() if direction == "desc" else column.asc()


def _wrap(query, offset, limit, sorts, sort, order, serializer) -> dict:
    offset, limit = _page(offset, limit)
    total = query.count()
    order_by = _sort_for(sorts, sort, order)
    rows = query.order_by(order_by, sorts["id"].asc()).limit(limit).offset(offset).all()
    return {
        "items": [serializer(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total,
    }


# ---------------------------------------------------------------------------
# serializers
# ---------------------------------------------------------------------------

def artist_json(artist: Artist, album_count=None, track_count=None) -> dict:
    out = {
        "id": artist.id,
        "name": artist.name,
        "image_url": artist.image_url,
        "monitored": bool(artist.monitored),
        "created_at": _iso(artist.created_at),
        # Opaque, provider-scoped identity: meaningful only to the provider
        # that supplied it; never a primary reference.
        "provider_id": artist.provider_id,
        "external_id": artist.external_id,
    }
    if album_count is not None:
        out["album_count"] = int(album_count)
    if track_count is not None:
        out["track_count"] = int(track_count)
    return out


def album_json(album: Album, track_count=None, downloaded_count=None) -> dict:
    out = {
        "id": album.id,
        "artist_id": album.artist_id,
        "artist_name": getattr(album.artist, "name", None),
        "name": album.name,
        "year": album.year,
        "cover_url": album.cover_url,
        "record_type": album.record_type,
        "is_downloaded": bool(album.is_downloaded),
        "monitored": bool(album.monitored),
        "created_at": _iso(album.created_at),
        "provider_id": album.provider_id,
        "external_id": album.external_id,
    }
    if track_count is not None:
        out["track_count"] = int(track_count)
    if downloaded_count is not None:
        out["downloaded_count"] = int(downloaded_count)
    return out


def track_json(track: Track) -> dict:
    album = track.album
    artist = None
    if track.artist_id:
        artist = db.session.get(Artist, track.artist_id)
    if artist is None and album is not None:
        artist = album.artist
    return {
        "id": track.id,
        "title": track.title,
        "album_id": track.album_id,
        "album_name": getattr(album, "name", None),
        "artist_id": getattr(artist, "id", None),
        "artist_name": getattr(artist, "name", None),
        "track_number": track.track_number,
        "disc_number": track.disc_number,
        "duration": track.duration,
        "genre": track.genre,
        "isrc": track.isrc,
        "status": track.status,
        "is_downloaded": bool(track.is_downloaded),
        "is_unmatched": bool(track.is_unmatched),
        "caution": bool(track.caution),
        "bitrate": track.bitrate,
        "file_format": track.file_format,
        "size_bytes": track.size_bytes,
        # Streaming metadata for a server integration (path on disk).
        "stream_path": track.local_path or track.file_path or None,
        "stream_ready": bool(track.is_downloaded and (track.local_path or track.file_path)),
        "created_at": _iso(track.created_at),
        "provider_id": track.provider_id,
        "external_id": track.external_id,
    }


# ---------------------------------------------------------------------------
# searches / listings
# ---------------------------------------------------------------------------

def artists(q=None, offset=0, limit=DEFAULT_PAGE, sort="name", order="asc") -> dict:
    """Search/list artists by name (case-insensitive substring)."""
    query = Artist.query
    if q:
        query = query.filter(Artist.name.ilike(f"%{q}%"))
    return _wrap(query, offset, limit, _ARTIST_SORTS, sort, order, artist_json)


def albums(q=None, artist_id=None, offset=0, limit=DEFAULT_PAGE,
           sort="name", order="asc") -> dict:
    """Search/list albums, optionally scoped to one artist."""
    query = Album.query
    if artist_id is not None:
        query = query.filter(Album.artist_id == _int_arg(artist_id, "artist_id"))
    if q:
        query = query.filter(Album.name.ilike(f"%{q}%"))
    return _wrap(query, offset, limit, _ALBUM_SORTS, sort, order, album_json)


def tracks(q=None, album_id=None, artist_id=None, downloaded=None, status=None,
           offset=0, limit=DEFAULT_PAGE, sort="title", order="asc") -> dict:
    """Search/list tracks with optional album/artist/state filters.

    ``q`` matches the track title, its album name or its artist name so a
    client can search generically without knowing which level it needs.
    """
    query = Track.query
    if album_id is not None:
        query = query.filter(Track.album_id == _int_arg(album_id, "album_id"))
    if artist_id is not None:
        query = query.filter(Track.artist_id == _int_arg(artist_id, "artist_id"))
    if downloaded is not None:
        query = query.filter(Track.is_downloaded.is_(bool(downloaded)))
    if status:
        query = query.filter(Track.status == str(status))
    if q:
        like = f"%{q}%"
        query = (query.outerjoin(Album, Track.album_id == Album.id)
                 .outerjoin(Artist, Track.artist_id == Artist.id)
                 .filter(or_(Track.title.ilike(like), Album.name.ilike(like),
                             Artist.name.ilike(like))))
    return _wrap(query, offset, limit, _TRACK_SORTS, sort, order, track_json)


# ---------------------------------------------------------------------------
# direct lookups
# ---------------------------------------------------------------------------

def get_artist(artist_id) -> dict | None:
    """One artist with its album/track counts (single round of queries)."""
    artist = db.session.get(Artist, _int_arg(artist_id, "artist_id"))
    if artist is None:
        return None
    album_count = Album.query.filter_by(artist_id=artist.id).count()
    track_count = Track.query.filter_by(artist_id=artist.id).count()
    return artist_json(artist, album_count=album_count, track_count=track_count)


def get_album(album_id) -> dict | None:
    """One album with its track and downloaded counts."""
    album = db.session.get(Album, _int_arg(album_id, "album_id"))
    if album is None:
        return None
    track_count = Track.query.filter_by(album_id=album.id).count()
    downloaded_count = Track.query.filter_by(album_id=album.id,
                                             is_downloaded=True).count()
    return album_json(album, track_count=track_count, downloaded_count=downloaded_count)


def get_track(track_id) -> dict | None:
    """One track by its stable internal id."""
    track = db.session.get(Track, _int_arg(track_id, "track_id"))
    return track_json(track) if track is not None else None


def stats() -> dict:
    """Library counts + how much of it is downloaded (cheap, indexed)."""
    return {
        "artists": Artist.query.count(),
        "albums": Album.query.count(),
        "tracks": Track.query.count(),
        "downloaded_tracks": Track.query.filter_by(is_downloaded=True).count(),
        "albums_downloaded": Album.query.filter_by(is_downloaded=True).count(),
    }


def _int_arg(value, name) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise LibraryQueryError(f"{name} must be an integer, got {value!r}")


def _iso(value):
    return value.isoformat() if value is not None else None
