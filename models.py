"""SQLAlchemy models for fnack: Artist, Album, Track, DownloadJob, AppSetting."""

from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Artist(db.Model):
    __tablename__ = "artists"
    __table_args__ = (
        # An artist's external identity is unique only within the namespace
        # of the provider that supplied it — two different providers may use
        # the same external id for different entities without colliding.
        db.Index("uq_artists_provider_external", "provider_id", "external_id", unique=True),
    )

    id = db.Column(db.Integer, primary_key=True)
    # Provider-scoped external identity (provider-neutral): `provider_id` is
    # the plugin id of the metadata provider that supplied `external_id` (the
    # opaque id THAT provider uses — e.g. a Deezer artist id when the
    # fnack.deezer-batch provider supplied it, or a prefixed self-identity
    # like "acoustid:<name>" / "lidarr:<name>" for artists created from a
    # single unknown track). provider_id is NULL only for self-created
    # identities. Core never interprets either value — the provider chain
    # owns parsing/conversion.
    provider_id = db.Column(db.String(64), nullable=True, index=True)
    external_id = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(256), nullable=False, index=True)
    image_url = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    monitored = db.Column(db.Boolean, default=True, nullable=False, index=True)
    auto_download = db.Column(db.Boolean, default=False, nullable=False)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    source = db.Column(db.String(16), default="manual", nullable=False)  # manual | folder | lidarr
    sync_status = db.Column(db.String(20), default="ready", nullable=False, index=True)  # ready | syncing | error
    sync_error = db.Column(db.Text, nullable=True)

    # Filter preferences
    filter_remixes = db.Column(db.Boolean, default=True, nullable=False)
    filter_lofi = db.Column(db.Boolean, default=True, nullable=False)
    filter_live = db.Column(db.Boolean, default=True, nullable=False)
    filter_compilations = db.Column(db.Boolean, default=True, nullable=False)
    include_albums = db.Column(db.Boolean, default=True, nullable=False)
    include_singles = db.Column(db.Boolean, default=True, nullable=False)
    include_compilations = db.Column(db.Boolean, default=False, nullable=False)

    albums = db.relationship(
        "Album",
        back_populates="artist",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    jobs = db.relationship(
        "DownloadJob",
        back_populates="artist",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    # Denormalized per-artist counters (Phase 1, scale-to-millions research).
    # Kept in sync at every write point where Album/Track rows change or
    # Track.is_downloaded flips — /api/artists reads these instead of full
    # GROUP BY scans on every request. Backfilled once at migration.
    total_albums = db.Column(db.Integer, default=0, nullable=False)
    total_tracks = db.Column(db.Integer, default=0, nullable=False)
    downloaded_tracks = db.Column(db.Integer, default=0, nullable=False)


class Album(db.Model):
    __tablename__ = "albums"

    id = db.Column(db.Integer, primary_key=True)
    artist_id = db.Column(db.Integer, db.ForeignKey("artists.id"), nullable=False, index=True)
    name = db.Column(db.String(512), nullable=False, index=True)
    year = db.Column(db.Integer, nullable=True)
    cover_url = db.Column(db.Text, nullable=True)
    # Provider-scoped external release identity: `provider_id` = the plugin
    # id of the metadata provider that supplied this release's `external_id`
    # (both opaque; interpreted only by that provider). NULL = self-created.
    provider_id = db.Column(db.String(64), nullable=True, index=True)
    external_id = db.Column(db.String(64), nullable=True, index=True)
    record_type = db.Column(db.String(32), default="album", nullable=False, index=True)  # album, single, compile, ep, other
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_downloaded = db.Column(db.Boolean, default=False, nullable=False, index=True)
    monitored = db.Column(db.Boolean, default=True, nullable=False, index=True)
    size_bytes = db.Column(db.BigInteger, default=0, nullable=False)
    local_path = db.Column(db.Text, nullable=True)

    # MusicBrainz enrichment (additive only; enrichment is provider-owned)
    mb_release_group_id = db.Column(db.String(64), nullable=True, index=True)
    mb_title = db.Column(db.String(512), nullable=True)
    mb_year = db.Column(db.Integer, nullable=True)
    mb_checked_at = db.Column(db.DateTime, nullable=True)

    artist = db.relationship("Artist", back_populates="albums")
    tracks = db.relationship(
        "Track",
        back_populates="album",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )


class Track(db.Model):
    __tablename__ = "tracks"

    id = db.Column(db.Integer, primary_key=True)
    album_id = db.Column(db.Integer, db.ForeignKey("albums.id"), nullable=False, index=True)
    artist_id = db.Column(db.Integer, db.ForeignKey("artists.id"), nullable=True, index=True)
    title = db.Column(db.String(512), nullable=False, index=True)
    track_number = db.Column(db.Integer, nullable=True)
    disc_number = db.Column(db.Integer, default=1, nullable=True)
    isrc = db.Column(db.String(64), nullable=True, index=True)
    # Provider-scoped external track identity: `provider_id` = the plugin id
    # of the metadata provider that supplied this track's `external_id` (both
    # opaque; interpreted only by that provider). NULL = self-created.
    provider_id = db.Column(db.String(64), nullable=True, index=True)
    external_id = db.Column(db.String(64), nullable=True, index=True)
    spotify_url = db.Column(db.Text, nullable=True)
    genre = db.Column(db.String(128), nullable=True)
    file_path = db.Column(db.Text, default="", nullable=False)
    file_format = db.Column(db.String(16), nullable=True)
    bitrate = db.Column(db.Integer, nullable=True)
    duration = db.Column(db.Float, nullable=True)  # in seconds (expected)
    status = db.Column(
        db.String(32), default="missing", nullable=False, index=True
    )  # missing, queued, downloading, completed, failed
    monitored = db.Column(db.Boolean, default=True, nullable=False, index=True)
    progress = db.Column(db.Float, default=0.0)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_downloaded = db.Column(db.Boolean, default=False, nullable=False, index=True)
    size_bytes = db.Column(db.BigInteger, default=0, nullable=False)
    local_path = db.Column(db.Text, nullable=True)
    is_unmatched = db.Column(db.Boolean, default=False, nullable=False)

    # AcoustID caution flag: file kept but AcoustID says it's a different song.
    caution = db.Column(db.Boolean, default=False, nullable=False, index=True)
    caution_info = db.Column(db.Text, nullable=True)  # what AcoustID matched it to

    album = db.relationship("Album", back_populates="tracks")


class DownloadJob(db.Model):
    __tablename__ = "download_jobs"
    __table_args__ = (
        # Scale (millions of job rows accumulate): the queue page reads the
        # active set ordered by creation and history ordered by recency —
        # composite indexes keep those reads on the index instead of sorting
        # the whole table every poll.
        db.Index("idx_jobs_status_created", "status", "created_at"),
        db.Index("idx_jobs_status_updated", "status", "updated_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    track_id = db.Column(db.Integer, db.ForeignKey("tracks.id"), nullable=True, index=True)
    album_id = db.Column(db.Integer, db.ForeignKey("albums.id"), nullable=True, index=True)
    artist_id = db.Column(db.Integer, db.ForeignKey("artists.id"), nullable=False, index=True)
    item_type = db.Column(db.String(16), default="track", nullable=False)  # track | album
    album_external_id = db.Column(db.String(64), nullable=True)
    album_name = db.Column(db.String(512), nullable=False)
    album_type = db.Column(db.String(32), default="album", nullable=False)
    album_url = db.Column(db.Text, default="", nullable=False)
    cover_url = db.Column(db.Text, nullable=True)
    status = db.Column(
        db.String(32), default="queued", nullable=False, index=True
    )  # queued, downloading, completed, failed, cancelled, skipped
    progress = db.Column(db.Float, default=0.0)
    track_count = db.Column(db.Integer, default=1)
    tracks_completed = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text, nullable=True)
    source = db.Column(db.String(16), default="manual", nullable=False)  # manual | auto | lidarr
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        index=True,
    )

    artist = db.relationship("Artist", back_populates="jobs")
    track = db.relationship("Track", foreign_keys=[track_id])


class AppSetting(db.Model):
    __tablename__ = "app_settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(256), nullable=False)


class User(db.Model):
    """fnack user account (whole-app login).

    password_hash is a werkzeug scrypt hash — salted, one-way, never
    plaintext (services/accounts.py). role: 'admin' (account management) or
    'user'. The first account (created on /setup at first boot) is admin.
    """

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(16), default="user", nullable=False)  # admin | user
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Accounts: programmatic credentials
# ---------------------------------------------------------------------------

class ApiToken(db.Model):
    """Account-scoped API credential for programmatic clients.

    Only the SHA-256 hash of the token is stored (`token_hash`), so a leaked
    database does not yield usable credentials; `prefix` keeps the first
    characters for identification in listings. A token belongs to exactly one
    account and inherits that account's identity on every request.

    Deliberately generic: this is fnack's own credential type, not any
    particular client protocol's.
    """

    __tablename__ = "api_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    label = db.Column(db.String(128), nullable=True)
    prefix = db.Column(db.String(16), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("api_tokens", cascade="all, delete-orphan"))


# ---------------------------------------------------------------------------
# Per-account user state
#
# Every table below is explicitly scoped by `user_id` and references the
# library through fnack's INTERNAL Artist/Album/Track ids. Those id columns
# intentionally carry NO foreign-key constraint: user state must outlive
# library churn (a track row deleted by a discography sync, a provider
# replaced) instead of cascading away or blocking the delete. Each row also
# keeps a small snapshot (title/artist/album/duration/isrc) so it stays
# readable while the referenced object is not resolvable, and can be
# re-attached to a re-created object through its ISRC.
# ---------------------------------------------------------------------------

class Playlist(db.Model):
    """An account-owned, ordered list of library tracks."""

    __tablename__ = "playlists"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(256), nullable=False)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    items = db.relationship(
        "PlaylistItem",
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistItem.position",
    )


class PlaylistItem(db.Model):
    """One track entry inside a playlist.

    `position` is unique per playlist (deterministic ordering, no duplicate or
    conflicting positions) and always compacted to 0..n-1 by the service.
    """

    __tablename__ = "playlist_items"
    __table_args__ = (
        db.UniqueConstraint("playlist_id", "position", name="uq_playlist_items_position"),
    )

    id = db.Column(db.Integer, primary_key=True)
    playlist_id = db.Column(db.Integer, db.ForeignKey("playlists.id"), nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False, index=True)
    track_id = db.Column(db.Integer, nullable=True, index=True)  # no FK: state outlives tracks
    # Snapshot of the referenced track at write time (also the re-link key).
    title = db.Column(db.String(512), nullable=True)
    artist_name = db.Column(db.String(512), nullable=True)
    album_name = db.Column(db.String(512), nullable=True)
    duration = db.Column(db.Float, nullable=True)
    isrc = db.Column(db.String(64), nullable=True, index=True)
    added_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    playlist = db.relationship("Playlist", back_populates="items")


class Favorite(db.Model):
    """A starred library object (artist, album or track) for one account."""

    __tablename__ = "favorites"
    __table_args__ = (
        db.UniqueConstraint("user_id", "item_type", "item_id", name="uq_favorites_item"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    item_type = db.Column(db.String(16), nullable=False, index=True)  # artist | album | track
    item_id = db.Column(db.Integer, nullable=False, index=True)
    name = db.Column(db.String(512), nullable=True)   # snapshot for display
    isrc = db.Column(db.String(64), nullable=True, index=True)  # tracks only; re-link key
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Rating(db.Model):
    """A 1..5 rating of a library object for one account."""

    __tablename__ = "ratings"
    __table_args__ = (
        db.UniqueConstraint("user_id", "item_type", "item_id", name="uq_ratings_item"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    item_type = db.Column(db.String(16), nullable=False, index=True)  # artist | album | track
    item_id = db.Column(db.Integer, nullable=False, index=True)
    rating = db.Column(db.Integer, nullable=False)  # 1..5
    isrc = db.Column(db.String(64), nullable=True, index=True)  # tracks only; re-link key
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Bookmark(db.Model):
    """A per-account playback position inside a track (or album)."""

    __tablename__ = "bookmarks"
    __table_args__ = (
        db.UniqueConstraint("user_id", "item_type", "item_id", name="uq_bookmarks_item"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    item_type = db.Column(db.String(16), nullable=False, index=True)  # track | album
    item_id = db.Column(db.Integer, nullable=False, index=True)
    position_ms = db.Column(db.Integer, nullable=False, default=0)
    comment = db.Column(db.Text, nullable=True)
    title = db.Column(db.String(512), nullable=True)          # snapshot
    isrc = db.Column(db.String(64), nullable=True, index=True)  # tracks only; re-link key
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Scrobble(db.Model):
    """One playback event recorded for an account (play history)."""

    __tablename__ = "scrobbles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    track_id = db.Column(db.Integer, nullable=True, index=True)  # no FK: history outlives tracks
    # Snapshot so history stays meaningful after library churn.
    title = db.Column(db.String(512), nullable=True)
    artist_name = db.Column(db.String(512), nullable=True)
    album_name = db.Column(db.String(512), nullable=True)
    duration = db.Column(db.Float, nullable=True)
    isrc = db.Column(db.String(64), nullable=True, index=True)
    played_at = db.Column(db.DateTime, nullable=False, index=True,
                          default=lambda: datetime.now(timezone.utc))
    submission = db.Column(db.Boolean, nullable=False, default=True)  # reported vs started
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class PlayQueue(db.Model):
    """The account's saved play queue (one per account) with its cursor."""

    __tablename__ = "play_queues"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True, index=True)
    current_index = db.Column(db.Integer, nullable=False, default=0)
    position_ms = db.Column(db.Integer, nullable=False, default=0)
    changed_by = db.Column(db.String(128), nullable=True)
    changed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    entries = db.relationship(
        "PlayQueueEntry",
        back_populates="queue",
        cascade="all, delete-orphan",
        order_by="PlayQueueEntry.position",
    )


class PlayQueueEntry(db.Model):
    """One track in a saved queue; `position` is unique per account."""

    __tablename__ = "play_queue_entries"
    __table_args__ = (
        db.UniqueConstraint("user_id", "position", name="uq_play_queue_entries_position"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    queue_id = db.Column(db.Integer, db.ForeignKey("play_queues.id"), nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False, index=True)
    track_id = db.Column(db.Integer, nullable=True, index=True)  # no FK: state outlives tracks
    title = db.Column(db.String(512), nullable=True)
    artist_name = db.Column(db.String(512), nullable=True)
    album_name = db.Column(db.String(512), nullable=True)
    duration = db.Column(db.Float, nullable=True)
    isrc = db.Column(db.String(64), nullable=True, index=True)
    added_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    queue = db.relationship("PlayQueue", back_populates="entries")
