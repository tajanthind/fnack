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


