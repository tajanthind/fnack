"""Generic per-account user state: playlists, favorites, ratings, bookmarks,
scrobble history and the saved play queue.

Design rules (see docs/integration-api.md):

* **Account-scoped.** Every function takes ``user_id`` first and every query
  filters by it. A row that belongs to another account is reported as
  ``NotFound`` — existence is never leaked across accounts.
* **Stable references.** State stores fnack's INTERNAL ``Artist``/``Album``/
  ``Track`` ids. Provider identities are never used as primary references and
  are never required to resolve a saved item.
* **Outlives the library.** The id columns carry no foreign-key constraint, so
  deleting a track (a discography sync prune, an explicit artist delete) can
  never cascade away or block user state. Each row keeps a small snapshot
  (title/artist/album/duration/isrc) so it stays readable while the referenced
  object is missing, and ``relink_tracks()`` re-attaches state to a re-created
  object through its ISRC (the provider-neutral recording identifier).
* **Validated.** Every referenced library id is checked to exist before it is
  persisted; unknown ids raise ``ValidationError``.
* **Deterministic ordering.** Playlist and queue positions are unique per
  owner and compacted to ``0..n-1`` after every mutation, so ordering never
  depends on insertion history.

This module is provider-neutral: it imports no provider implementation and
knows nothing about any client protocol.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case
from sqlalchemy.orm import joinedload

from models import (
    Album,
    Artist,
    Bookmark,
    Favorite,
    Playlist,
    PlaylistItem,
    PlayQueue,
    PlayQueueEntry,
    Rating,
    Scrobble,
    Track,
    User,
    db,
)

ITEM_TYPES = ("artist", "album", "track")
BOOKMARK_TYPES = ("album", "track")
MAX_RATING = 5
DEFAULT_PAGE = 100
MAX_PAGE = 500

# Positions are moved to this offset during two-phase reordering so the
# per-owner uniqueness constraint is never transiently violated.
_POSITION_SHIFT = 1_000_000

_UNSET = object()


class UserStateError(Exception):
    """Base class for user-state domain errors."""


class NotFound(UserStateError):
    """The object does not exist, or is not owned by this account."""


class ValidationError(UserStateError):
    """The request is malformed or references unknown library objects."""


# ---------------------------------------------------------------------------
# validation + snapshot helpers
# ---------------------------------------------------------------------------

def _model_for(item_type: str):
    mapping = {"artist": Artist, "album": Album, "track": Track}
    try:
        return mapping[item_type]
    except KeyError:
        raise ValidationError(
            f"unknown item_type {item_type!r} (expected one of {', '.join(ITEM_TYPES)})")


def _require_account(user_id) -> int:
    if not user_id:
        raise ValidationError("an account is required")
    if db.session.get(User, user_id) is None:
        raise NotFound(f"no such account: {user_id}")
    return int(user_id)


def require_library_object(item_type: str, item_id) -> object:
    """Validate that a library object of ``item_type`` with ``item_id`` exists.

    Raises ValidationError (unknown id) — used before persisting any reference.
    """
    model = _model_for(item_type)
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        raise ValidationError(f"item_id must be an integer, got {item_id!r}")
    obj = db.session.get(model, item_id)
    if obj is None:
        raise ValidationError(f"no {item_type} with id {item_id}")
    return obj


def _artist_of(track: Track):
    """The track's artist: its own artist_id, else the album's artist."""
    if getattr(track, "artist_id", None):
        artist = db.session.get(Artist, track.artist_id)
        if artist is not None:
            return artist
    album = track.album
    return album.artist if album is not None else None


def _track_snapshot(track: Track) -> dict:
    album = track.album
    artist = _artist_of(track)
    return {
        "title": track.title,
        "artist_name": getattr(artist, "name", None),
        "album_name": getattr(album, "name", None),
        "duration": track.duration,
        "isrc": track.isrc,
    }


def _snapshot_for(item_type: str, obj) -> dict:
    """Snapshot fields for any supported library object type."""
    if item_type == "track":
        return _track_snapshot(obj)
    return {"title": getattr(obj, "name", None), "artist_name": None,
            "album_name": None, "duration": None, "isrc": None}


def _page(offset, limit) -> tuple[int, int]:
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(limit) if limit is not None else DEFAULT_PAGE
    except (TypeError, ValueError):
        limit = DEFAULT_PAGE
    limit = max(1, min(limit, MAX_PAGE))
    return offset, limit


def _live_tracks(track_ids) -> dict:
    """{track_id: json dict} for ids that still exist in the library."""
    ids = [int(i) for i in {t for t in track_ids if t is not None}]
    if not ids:
        return {}
    rows = (Track.query
            .options(joinedload(Track.album).joinedload(Album.artist))
            .filter(Track.id.in_(ids)).all())
    out = {}
    for t in rows:
        album = t.album
        artist = _artist_of(t)
        out[t.id] = {
            "id": t.id,
            "title": t.title,
            "album_id": t.album_id,
            "album_name": getattr(album, "name", None),
            "artist_id": getattr(artist, "id", None),
            "artist_name": getattr(artist, "name", None),
            "duration": t.duration,
            "track_number": t.track_number,
            "disc_number": t.disc_number,
            "isrc": t.isrc,
            "is_downloaded": bool(t.is_downloaded),
        }
    return out


def _resolve_entry(row, live: dict) -> dict:
    """A state entry with its live track merged in (or a snapshot when gone)."""
    entry = {
        "track_id": row.track_id,
        "title": row.title,
        "artist_name": row.artist_name,
        "album_name": row.album_name,
        "duration": row.duration,
        "isrc": row.isrc,
        "available": False,
    }
    if row.track_id is not None and row.track_id in live:
        track = dict(live[row.track_id])
        entry.update(track)
        entry["available"] = True
        # Live library metadata wins; snapshots are only the fallback.
        if track.get("title"):
            entry["title"] = track["title"]
        if track.get("artist_name"):
            entry["artist_name"] = track["artist_name"]
        if track.get("album_name"):
            entry["album_name"] = track["album_name"]
    return entry


# ---------------------------------------------------------------------------
# playlists
# ---------------------------------------------------------------------------

def _playlist_for(user_id, playlist_id) -> Playlist:
    try:
        playlist_id = int(playlist_id)
    except (TypeError, ValueError):
        raise ValidationError(f"playlist_id must be an integer, got {playlist_id!r}")
    playlist = Playlist.query.filter_by(id=playlist_id, user_id=int(user_id)).first()
    if playlist is None:
        # Same error whether it does not exist or belongs to someone else.
        raise NotFound(f"no playlist {playlist_id} for this account")
    return playlist


def _ordered_items(playlist: Playlist) -> list:
    return sorted(playlist.items, key=lambda i: (i.position, i.id))


def _assign_positions(playlist_id: int, ordered_ids: list) -> None:
    """Write 0..n-1 positions for ``ordered_ids`` in ONE statement.

    Uses a CASE expression rather than per-object assignment on purpose: the
    bulk shift below runs outside the ORM, so in-session objects still hold
    their pre-shift values — assigning through them would look like a no-op
    for any row whose new position happens to equal its stale value and the
    UPDATE would silently be skipped.
    """
    if not ordered_ids:
        return
    mapping = {int(item_id): position for position, item_id in enumerate(ordered_ids)}
    db.session.query(PlaylistItem).filter(
        PlaylistItem.playlist_id == playlist_id,
        PlaylistItem.id.in_(list(mapping)),
    ).update({PlaylistItem.position: case(mapping, value=PlaylistItem.id)},
             synchronize_session=False)
    db.session.flush()
    db.session.expire_all()


def _write_positions(playlist: Playlist, ordered_ids: list) -> None:
    """Assign 0..n-1 positions following ``ordered_ids`` without ever leaving
    two rows on the same position (two-phase move through _POSITION_SHIFT)."""
    items = {item.id: item for item in playlist.items}
    unknown = [i for i in ordered_ids if i not in items]
    if unknown:
        raise ValidationError(f"unknown playlist item ids: {unknown}")
    db.session.query(PlaylistItem).filter_by(playlist_id=playlist.id).update(
        {PlaylistItem.position: PlaylistItem.position + _POSITION_SHIFT},
        synchronize_session=False,
    )
    db.session.flush()
    _assign_positions(playlist.id, ordered_ids)


def _insert_items(playlist: Playlist, track_ids, position=None) -> list:
    """Insert tracks at ``position`` (or append) with deterministic order.

    Three phases so the per-playlist uniqueness constraint is never violated,
    not even transiently by an autoflush: existing rows move into the safe
    high range, new rows are inserted above them, then every row is assigned
    its final 0..n-1 position in one pass.
    """
    tracks = []
    for track_id in track_ids:
        try:
            track_id = int(track_id)
        except (TypeError, ValueError):
            raise ValidationError(f"track_id must be an integer, got {track_id!r}")
        track = db.session.get(Track, track_id)
        if track is None:
            raise ValidationError(f"no track with id {track_id}")
        tracks.append(track)

    ordered = _ordered_items(playlist)
    if position is None:
        insert_at = len(ordered)
    else:
        try:
            insert_at = int(position)
        except (TypeError, ValueError):
            raise ValidationError(f"position must be an integer, got {position!r}")
        insert_at = max(0, min(insert_at, len(ordered)))

    # 1) existing rows out of the way
    db.session.query(PlaylistItem).filter_by(playlist_id=playlist.id).update(
        {PlaylistItem.position: PlaylistItem.position + _POSITION_SHIFT},
        synchronize_session=False)
    db.session.flush()

    # 2) new rows above everything currently stored
    new_rows = []
    for offset, track in enumerate(tracks):
        row = PlaylistItem(playlist_id=playlist.id,
                           position=_POSITION_SHIFT * 2 + offset,
                           track_id=track.id, **_track_snapshot(track))
        db.session.add(row)
        new_rows.append(row)
    db.session.flush()

    # 3) final compact order (single CASE statement — see _assign_positions)
    final_order = ([item.id for item in ordered[:insert_at]]
                   + [row.id for row in new_rows]
                   + [item.id for item in ordered[insert_at:]])
    _assign_positions(playlist.id, final_order)
    return new_rows


def create_playlist(user_id, name, comment=None, track_ids=None) -> dict:
    """Create a playlist owned by ``user_id`` (optionally pre-filled)."""
    user_id = _require_account(user_id)
    name = (name or "").strip()
    if not name:
        raise ValidationError("playlist name is required")
    if len(name) > 256:
        raise ValidationError("playlist name is too long (max 256 characters)")
    playlist = Playlist(user_id=user_id, name=name, comment=comment)
    db.session.add(playlist)
    db.session.flush()
    if track_ids:
        _insert_items(playlist, track_ids)
    db.session.commit()
    return get_playlist(user_id, playlist.id)


def list_playlists(user_id, offset=0, limit=DEFAULT_PAGE) -> dict:
    """Playlists owned by this account (metadata + item counts)."""
    user_id = _require_account(user_id)
    offset, limit = _page(offset, limit)
    query = Playlist.query.filter_by(user_id=user_id)
    total = query.count()
    rows = (query.order_by(Playlist.name.asc(), Playlist.id.asc())
            .limit(limit).offset(offset).all())
    counts = dict(
        db.session.query(PlaylistItem.playlist_id, db.func.count(PlaylistItem.id))
        .filter(PlaylistItem.playlist_id.in_([p.id for p in rows] or [0]))
        .group_by(PlaylistItem.playlist_id).all()
    ) if rows else {}
    return {
        "items": [{
            "id": p.id,
            "name": p.name,
            "comment": p.comment,
            "item_count": int(counts.get(p.id, 0)),
            "created_at": _iso(p.created_at),
            "updated_at": _iso(p.updated_at),
        } for p in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def get_playlist(user_id, playlist_id, offset=0, limit=None) -> dict:
    """One playlist with its items resolved against the live library."""
    user_id = _require_account(user_id)
    playlist = _playlist_for(user_id, playlist_id)
    items = _ordered_items(playlist)
    total = len(items)
    if limit is not None:
        offset, limit = _page(offset, limit)
        page_items = items[offset:offset + limit]
    else:
        offset, page_items = 0, items
    live = _live_tracks([i.track_id for i in page_items])
    return {
        "id": playlist.id,
        "name": playlist.name,
        "comment": playlist.comment,
        "created_at": _iso(playlist.created_at),
        "updated_at": _iso(playlist.updated_at),
        "total": total,
        "offset": offset,
        "items": [dict(_resolve_entry(i, live), item_id=i.id, position=i.position)
                  for i in page_items],
    }


def update_playlist(user_id, playlist_id, name=_UNSET, comment=_UNSET) -> dict:
    """Rename/annotate a playlist. Unset arguments are left unchanged."""
    user_id = _require_account(user_id)
    playlist = _playlist_for(user_id, playlist_id)
    if name is not _UNSET:
        name = (name or "").strip()
        if not name:
            raise ValidationError("playlist name is required")
        if len(name) > 256:
            raise ValidationError("playlist name is too long (max 256 characters)")
        playlist.name = name
    if comment is not _UNSET:
        playlist.comment = comment
    db.session.commit()
    return get_playlist(user_id, playlist.id)


def delete_playlist(user_id, playlist_id) -> bool:
    user_id = _require_account(user_id)
    playlist = _playlist_for(user_id, playlist_id)
    db.session.delete(playlist)
    db.session.commit()
    return True


def add_tracks(user_id, playlist_id, track_ids, position=None) -> dict:
    """Append (or insert at ``position``) tracks; ordering stays compact."""
    user_id = _require_account(user_id)
    playlist = _playlist_for(user_id, playlist_id)
    if not track_ids:
        raise ValidationError("track_ids is required")
    _insert_items(playlist, track_ids, position=position)
    db.session.commit()
    return get_playlist(user_id, playlist.id)


def remove_item(user_id, playlist_id, item_id) -> dict:
    """Remove one entry and re-compact the remaining positions."""
    user_id = _require_account(user_id)
    playlist = _playlist_for(user_id, playlist_id)
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        raise ValidationError(f"item_id must be an integer, got {item_id!r}")
    item = PlaylistItem.query.filter_by(id=item_id, playlist_id=playlist.id).first()
    if item is None:
        raise NotFound(f"no playlist item {item_id} in playlist {playlist.id}")
    db.session.delete(item)
    db.session.flush()
    remaining = [i.id for i in _ordered_items(playlist) if i.id != item_id]
    _write_positions(playlist, remaining)
    db.session.commit()
    return get_playlist(user_id, playlist.id)


def reorder_items(user_id, playlist_id, item_ids) -> dict:
    """Set the full item order.

    ``item_ids`` must be exactly the playlist's item ids (no duplicates, no
    extras) — a partial or ambiguous order is refused instead of guessed.
    """
    user_id = _require_account(user_id)
    playlist = _playlist_for(user_id, playlist_id)
    ids = [int(i) for i in (item_ids or [])]
    existing = {i.id for i in playlist.items}
    if len(ids) != len(set(ids)):
        raise ValidationError("item_ids contains duplicates")
    if set(ids) != existing:
        raise ValidationError(
            "item_ids must contain exactly the playlist's item ids "
            f"(missing: {sorted(existing - set(ids))}, unknown: {sorted(set(ids) - existing)})")
    _write_positions(playlist, ids)
    db.session.commit()
    return get_playlist(user_id, playlist.id)


def set_items(user_id, playlist_id, track_ids) -> dict:
    """Replace a playlist's contents with ``track_ids`` in the given order."""
    user_id = _require_account(user_id)
    playlist = _playlist_for(user_id, playlist_id)
    for item in list(playlist.items):
        db.session.delete(item)
    db.session.flush()
    db.session.expire(playlist, ["items"])
    if track_ids:
        _insert_items(playlist, track_ids)
    db.session.commit()
    return get_playlist(user_id, playlist.id)


# ---------------------------------------------------------------------------
# favorites (stars)
# ---------------------------------------------------------------------------

def star(user_id, item_type, item_id) -> dict:
    """Star a library object (idempotent)."""
    user_id = _require_account(user_id)
    obj = require_library_object(item_type, item_id)
    snapshot = _snapshot_for(item_type, obj)
    row = Favorite.query.filter_by(user_id=user_id, item_type=item_type,
                                   item_id=int(item_id)).first()
    if row is None:
        row = Favorite(user_id=user_id, item_type=item_type, item_id=int(item_id))
        db.session.add(row)
    row.name = snapshot["title"]
    row.isrc = snapshot["isrc"]
    db.session.commit()
    return _favorite_json(row)


def unstar(user_id, item_type, item_id) -> bool:
    user_id = _require_account(user_id)
    _model_for(item_type)
    row = Favorite.query.filter_by(user_id=user_id, item_type=item_type,
                                   item_id=int(item_id)).first()
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def is_starred(user_id, item_type, item_id) -> bool:
    user_id = _require_account(user_id)
    _model_for(item_type)
    return Favorite.query.filter_by(user_id=user_id, item_type=item_type,
                                    item_id=int(item_id)).first() is not None


def list_favorites(user_id, item_type=None, offset=0, limit=DEFAULT_PAGE) -> dict:
    user_id = _require_account(user_id)
    offset, limit = _page(offset, limit)
    query = Favorite.query.filter_by(user_id=user_id)
    if item_type:
        _model_for(item_type)
        query = query.filter_by(item_type=item_type)
    total = query.count()
    rows = (query.order_by(Favorite.created_at.desc(), Favorite.id.desc())
            .limit(limit).offset(offset).all())
    live = _live_tracks([r.item_id for r in rows if r.item_type == "track"])
    return {
        "items": [_favorite_json(r, live) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def _favorite_json(row: Favorite, live=None) -> dict:
    live = live or {}
    available = _object_exists(row.item_type, row.item_id)
    out = {
        "item_type": row.item_type,
        "item_id": row.item_id,
        "name": row.name,
        "isrc": row.isrc,
        "created_at": _iso(row.created_at),
        "available": available,
    }
    if row.item_type == "track" and row.item_id in live:
        out["track"] = live[row.item_id]
    return out


def _object_exists(item_type: str, item_id) -> bool:
    model = _model_for(item_type)
    return db.session.get(model, item_id) is not None


# ---------------------------------------------------------------------------
# ratings
# ---------------------------------------------------------------------------

def set_rating(user_id, item_type, item_id, rating) -> dict:
    """Set (1..5) or clear (None/0) a rating for a library object."""
    user_id = _require_account(user_id)
    obj = require_library_object(item_type, item_id)
    row = Rating.query.filter_by(user_id=user_id, item_type=item_type,
                                 item_id=int(item_id)).first()
    if rating in (None, 0, "0", ""):
        if row is not None:
            db.session.delete(row)
            db.session.commit()
        return {"item_type": item_type, "item_id": int(item_id), "rating": None}
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        raise ValidationError(f"rating must be an integer 1..{MAX_RATING}, got {rating!r}")
    if not 1 <= rating <= MAX_RATING:
        raise ValidationError(f"rating must be between 1 and {MAX_RATING}")
    if row is None:
        row = Rating(user_id=user_id, item_type=item_type, item_id=int(item_id))
        db.session.add(row)
    row.rating = rating
    if item_type == "track":
        row.isrc = obj.isrc
    db.session.commit()
    return {"item_type": item_type, "item_id": int(item_id), "rating": rating,
            "updated_at": _iso(row.updated_at)}


def get_rating(user_id, item_type, item_id):
    user_id = _require_account(user_id)
    _model_for(item_type)
    row = Rating.query.filter_by(user_id=user_id, item_type=item_type,
                                 item_id=int(item_id)).first()
    return row.rating if row else None


def list_ratings(user_id, item_type=None, offset=0, limit=DEFAULT_PAGE) -> dict:
    user_id = _require_account(user_id)
    offset, limit = _page(offset, limit)
    query = Rating.query.filter_by(user_id=user_id)
    if item_type:
        _model_for(item_type)
        query = query.filter_by(item_type=item_type)
    total = query.count()
    rows = (query.order_by(Rating.updated_at.desc(), Rating.id.desc())
            .limit(limit).offset(offset).all())
    return {
        "items": [{"item_type": r.item_type, "item_id": r.item_id,
                   "rating": r.rating, "available": _object_exists(r.item_type, r.item_id),
                   "updated_at": _iso(r.updated_at)} for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# bookmarks
# ---------------------------------------------------------------------------

def set_bookmark(user_id, item_type, item_id, position_ms, comment=None) -> dict:
    """Create/update a playback position bookmark for this account."""
    user_id = _require_account(user_id)
    if item_type not in BOOKMARK_TYPES:
        raise ValidationError(
            f"bookmarks support {', '.join(BOOKMARK_TYPES)} (got {item_type!r})")
    obj = require_library_object(item_type, item_id)
    try:
        position_ms = int(position_ms)
    except (TypeError, ValueError):
        raise ValidationError("position_ms must be an integer")
    if position_ms < 0:
        raise ValidationError("position_ms must be >= 0")
    row = Bookmark.query.filter_by(user_id=user_id, item_type=item_type,
                                   item_id=int(item_id)).first()
    if row is None:
        row = Bookmark(user_id=user_id, item_type=item_type, item_id=int(item_id))
        db.session.add(row)
    row.position_ms = position_ms
    row.comment = comment
    row.title = getattr(obj, "title", None) or getattr(obj, "name", None)
    if item_type == "track":
        row.isrc = obj.isrc
    db.session.commit()
    return _bookmark_json(row)


def get_bookmark(user_id, item_type, item_id):
    user_id = _require_account(user_id)
    if item_type not in BOOKMARK_TYPES:
        raise ValidationError(f"bookmarks support {', '.join(BOOKMARK_TYPES)}")
    row = Bookmark.query.filter_by(user_id=user_id, item_type=item_type,
                                   item_id=int(item_id)).first()
    return _bookmark_json(row) if row else None


def delete_bookmark(user_id, item_type, item_id) -> bool:
    user_id = _require_account(user_id)
    if item_type not in BOOKMARK_TYPES:
        raise ValidationError(f"bookmarks support {', '.join(BOOKMARK_TYPES)}")
    row = Bookmark.query.filter_by(user_id=user_id, item_type=item_type,
                                   item_id=int(item_id)).first()
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def list_bookmarks(user_id, offset=0, limit=DEFAULT_PAGE) -> dict:
    user_id = _require_account(user_id)
    offset, limit = _page(offset, limit)
    query = Bookmark.query.filter_by(user_id=user_id)
    total = query.count()
    rows = (query.order_by(Bookmark.updated_at.desc(), Bookmark.id.desc())
            .limit(limit).offset(offset).all())
    return {
        "items": [_bookmark_json(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def _bookmark_json(row: Bookmark) -> dict:
    live = _live_tracks([row.item_id]) if row.item_type == "track" else {}
    return {
        "item_type": row.item_type,
        "item_id": row.item_id,
        "position_ms": row.position_ms,
        "comment": row.comment,
        "title": row.title,
        "isrc": row.isrc,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "available": bool(live) or _object_exists(row.item_type, row.item_id),
    }


# ---------------------------------------------------------------------------
# scrobble / play history
# ---------------------------------------------------------------------------

def record_scrobble(user_id, track_id, played_at=None, submission=True) -> dict:
    """Record a playback event for this account."""
    user_id = _require_account(user_id)
    track = require_library_object("track", track_id)
    snapshot = _track_snapshot(track)
    played_at = played_at or datetime.now(timezone.utc)
    if isinstance(played_at, str):
        played_at = _parse_datetime(played_at)
    row = Scrobble(user_id=user_id, track_id=track.id, played_at=played_at,
                   submission=bool(submission), **snapshot)
    db.session.add(row)
    db.session.commit()
    return _scrobble_json(row)


def list_scrobbles(user_id, offset=0, limit=DEFAULT_PAGE, since=None) -> dict:
    """Play history, newest first (``since`` = ISO-8601 timestamp)."""
    user_id = _require_account(user_id)
    offset, limit = _page(offset, limit)
    query = Scrobble.query.filter_by(user_id=user_id)
    if since:
        query = query.filter(Scrobble.played_at >= _parse_datetime(since))
    total = query.count()
    rows = (query.order_by(Scrobble.played_at.desc(), Scrobble.id.desc())
            .limit(limit).offset(offset).all())
    return {
        "items": [_scrobble_json(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def play_counts(user_id, limit=50) -> list:
    """Most-played tracks for this account: [{track_id, plays, title, ...}]."""
    user_id = _require_account(user_id)
    limit = max(1, min(int(limit or 50), MAX_PAGE))
    rows = (db.session.query(Scrobble.track_id, db.func.count(Scrobble.id).label("plays"))
            .filter(Scrobble.user_id == user_id, Scrobble.track_id.isnot(None))
            .group_by(Scrobble.track_id)
            .order_by(db.text("plays DESC"))
            .limit(limit).all())
    live = _live_tracks([r[0] for r in rows])
    out = []
    for track_id, plays in rows:
        entry = {"track_id": track_id, "plays": int(plays)}
        if track_id in live:
            entry.update(live[track_id])
            entry["available"] = True
        else:
            entry["available"] = False
        out.append(entry)
    return out


def _scrobble_json(row: Scrobble) -> dict:
    return {
        "id": row.id,
        "track_id": row.track_id,
        "title": row.title,
        "artist_name": row.artist_name,
        "album_name": row.album_name,
        "isrc": row.isrc,
        "played_at": _iso(row.played_at),
        "submission": bool(row.submission),
        "available": _object_exists("track", row.track_id) if row.track_id else False,
    }


# ---------------------------------------------------------------------------
# saved play queue
# ---------------------------------------------------------------------------

def save_queue(user_id, track_ids, current_index=0, position_ms=0, changed_by=None) -> dict:
    """Replace this account's saved queue."""
    user_id = _require_account(user_id)
    ids = []
    for track_id in (track_ids or []):
        try:
            track_id = int(track_id)
        except (TypeError, ValueError):
            raise ValidationError(f"track_id must be an integer, got {track_id!r}")
        if db.session.get(Track, track_id) is None:
            raise ValidationError(f"no track with id {track_id}")
        ids.append(track_id)
    try:
        current_index = int(current_index or 0)
    except (TypeError, ValueError):
        raise ValidationError("current_index must be an integer")
    try:
        position_ms = int(position_ms or 0)
    except (TypeError, ValueError):
        raise ValidationError("position_ms must be an integer")
    if ids:
        current_index = max(0, min(current_index, len(ids) - 1))
    else:
        current_index = 0

    queue = PlayQueue.query.filter_by(user_id=user_id).first()
    if queue is None:
        queue = PlayQueue(user_id=user_id)
        db.session.add(queue)
        db.session.flush()
    # Clear entries before re-inserting so the unique position constraint can
    # never see a duplicate during the swap.
    db.session.query(PlayQueueEntry).filter_by(queue_id=queue.id).delete(
        synchronize_session=False)
    db.session.flush()
    for position, track_id in enumerate(ids):
        track = db.session.get(Track, track_id)
        db.session.add(PlayQueueEntry(
            user_id=user_id, queue_id=queue.id, position=position,
            track_id=track_id, **_track_snapshot(track)))
    queue.current_index = current_index
    queue.position_ms = position_ms
    queue.changed_by = changed_by
    queue.changed_at = datetime.now(timezone.utc)
    db.session.commit()
    return get_queue(user_id)


def get_queue(user_id) -> dict:
    """This account's saved queue, entries resolved against the library."""
    user_id = _require_account(user_id)
    queue = PlayQueue.query.filter_by(user_id=user_id).first()
    if queue is None:
        return {"current_index": 0, "position_ms": 0, "changed_by": None,
                "changed_at": None, "total": 0, "items": []}
    rows = sorted(queue.entries, key=lambda e: (e.position, e.id))
    live = _live_tracks([e.track_id for e in rows])
    return {
        "current_index": queue.current_index,
        "position_ms": queue.position_ms,
        "changed_by": queue.changed_by,
        "changed_at": _iso(queue.changed_at),
        "updated_at": _iso(queue.updated_at),
        "total": len(rows),
        "items": [dict(_resolve_entry(r, live), position=r.position, entry_id=r.id)
                  for r in rows],
    }


def clear_queue(user_id) -> bool:
    user_id = _require_account(user_id)
    queue = PlayQueue.query.filter_by(user_id=user_id).first()
    if queue is None:
        return False
    db.session.query(PlayQueueEntry).filter_by(queue_id=queue.id).delete(
        synchronize_session=False)
    queue.current_index = 0
    queue.position_ms = 0
    queue.changed_at = datetime.now(timezone.utc)
    db.session.commit()
    return True


# ---------------------------------------------------------------------------
# library churn: keep state alive and re-attachable
# ---------------------------------------------------------------------------

def snapshot_referenced_tracks(track_ids) -> int:
    """Refresh snapshots for state that references tracks about to disappear.

    Call this BEFORE hard-deleting library rows (a discography sync prune, an
    artist delete). The state rows are never removed: they keep their internal
    id plus a readable snapshot, and can be re-attached later by ISRC.
    """
    ids = [int(i) for i in (track_ids or [])]
    if not ids:
        return 0
    tracks = {t.id: t for t in Track.query.filter(Track.id.in_(ids)).all()}
    if not tracks:
        return 0
    touched = 0
    for row in PlaylistItem.query.filter(PlaylistItem.track_id.in_(ids)).all():
        _apply_snapshot(row, tracks.get(row.track_id))
        touched += 1
    for row in PlayQueueEntry.query.filter(PlayQueueEntry.track_id.in_(ids)).all():
        _apply_snapshot(row, tracks.get(row.track_id))
        touched += 1
    for fav in Favorite.query.filter(Favorite.item_type == "track",
                                     Favorite.item_id.in_(ids)).all():
        track = tracks.get(fav.item_id)
        if track:
            fav.name = track.title
            fav.isrc = track.isrc
            touched += 1
    for rat in Rating.query.filter(Rating.item_type == "track",
                                   Rating.item_id.in_(ids)).all():
        track = tracks.get(rat.item_id)
        if track:
            rat.isrc = track.isrc
            touched += 1
    for bm in Bookmark.query.filter(Bookmark.item_type == "track",
                                    Bookmark.item_id.in_(ids)).all():
        track = tracks.get(bm.item_id)
        if track:
            bm.title = track.title
            bm.isrc = track.isrc
            touched += 1
    db.session.flush()
    return touched


def _apply_snapshot(row, track) -> None:
    if track is None:
        return
    snapshot = _track_snapshot(track)
    row.title = snapshot["title"]
    row.artist_name = snapshot["artist_name"]
    row.album_name = snapshot["album_name"]
    row.duration = snapshot["duration"]
    row.isrc = snapshot["isrc"]


def relink_tracks(track_ids) -> int:
    """Re-attach state to freshly created library tracks via ISRC.

    A provider change can delete and re-create the same recording under a new
    internal id. State that references the old id (and whose old id no longer
    resolves) is re-pointed at the new row when the ISRC matches — so a star,
    rating, bookmark, playlist entry or queued item survives the churn.
    """
    ids = [int(i) for i in (track_ids or [])]
    if not ids:
        return 0
    tracks = Track.query.filter(Track.id.in_(ids)).all()
    relinked = 0
    for track in tracks:
        if not track.isrc:
            continue
        candidate = {"title": track.title, "artist_name": None,
                     "album_name": None, "duration": track.duration,
                     "isrc": track.isrc}
        candidate["artist_name"] = getattr(_artist_of(track), "name", None)
        candidate["album_name"] = getattr(track.album, "name", None)
        stale = lambda tid: tid is not None and db.session.get(Track, tid) is None  # noqa: E731

        for row in PlaylistItem.query.filter_by(isrc=track.isrc).all():
            if stale(row.track_id):
                row.track_id = track.id
                _apply_snapshot(row, track)
                relinked += 1
        for row in PlayQueueEntry.query.filter_by(isrc=track.isrc).all():
            if stale(row.track_id):
                row.track_id = track.id
                _apply_snapshot(row, track)
                relinked += 1
        for fav in Favorite.query.filter_by(item_type="track", isrc=track.isrc).all():
            if stale(fav.item_id):
                fav.item_id = track.id
                fav.name = track.title
                relinked += 1
        for rat in Rating.query.filter_by(item_type="track", isrc=track.isrc).all():
            if stale(rat.item_id):
                rat.item_id = track.id
                relinked += 1
        for bm in Bookmark.query.filter_by(item_type="track", isrc=track.isrc).all():
            if stale(bm.item_id):
                bm.item_id = track.id
                bm.title = track.title
                relinked += 1
        for scrobble in Scrobble.query.filter_by(isrc=track.isrc).all():
            if stale(scrobble.track_id):
                scrobble.track_id = track.id
                relinked += 1
    db.session.flush()
    return relinked


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

def summary(user_id) -> dict:
    """Per-account state counts (cheap dashboard/integration call)."""
    user_id = _require_account(user_id)
    queue = PlayQueue.query.filter_by(user_id=user_id).first()
    return {
        "playlists": Playlist.query.filter_by(user_id=user_id).count(),
        "favorites": Favorite.query.filter_by(user_id=user_id).count(),
        "ratings": Rating.query.filter_by(user_id=user_id).count(),
        "bookmarks": Bookmark.query.filter_by(user_id=user_id).count(),
        "scrobbles": Scrobble.query.filter_by(user_id=user_id).count(),
        "queue_items": (PlayQueueEntry.query.filter_by(user_id=user_id).count()
                        if queue else 0),
    }


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _iso(value):
    return value.isoformat() if value is not None else None


def _parse_datetime(value: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValidationError(f"invalid ISO-8601 datetime: {value!r}")
