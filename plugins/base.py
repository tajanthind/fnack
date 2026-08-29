"""Plugin type interfaces.

Plugin authors subclass exactly one (or more, via multiple inheritance) of
the classes below. None of these classes — nor anything they're handed —
touches fnack's SQLAlchemy models or Flask app directly. The only thing a
plugin instance holds is `self.context`, a `PluginContext` (see context.py).

Keeping this file's public shapes stable *is* the API contract referenced
by a plugin manifest's `api_version`. Breaking changes here require bumping
`plugins.PLUGIN_API_VERSION`'s major component.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# --------------------------------------------------------------------------
# Plain data shapes passed across the plugin boundary. Never SQLAlchemy rows.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TrackRef:
    """Read-only view of a track, handed to downloader/fingerprint plugins."""
    id: int
    title: str
    artist_name: str
    album_name: str
    isrc: Optional[str] = None
    duration: Optional[float] = None       # expected duration, seconds
    spotify_url: Optional[str] = None
    deezer_id: Optional[str] = None
    disc_number: int = 1
    track_number: Optional[int] = None


@dataclass
class DownloadResult:
    success: bool
    file_path: Optional[Path] = None
    error: Optional[str] = None
    source_plugin_id: Optional[str] = None
    extra: dict = field(default_factory=dict)   # e.g. {"bitrate": 940, "format": "flac"}


@dataclass
class FingerprintResult:
    confidence: float                      # 0.0 - 1.0
    matched_title: Optional[str] = None
    matched_artist: Optional[str] = None
    matched_isrc: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class TaskResult:
    success: bool
    message: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class RecommendationItem:
    kind: str                              # "artist" | "album" | "track"
    ref_id: str                            # provider-specific id (e.g. deezer id)
    title: str
    subtitle: str = ""
    score: float = 0.0
    artwork_url: Optional[str] = None


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str
    type: list[str]
    api_version: str
    entry_point: str
    min_core_version: str = "0.0.0"
    author: str = ""
    description: str = ""
    homepage: str = ""
    permissions: list[str] = field(default_factory=list)
    settings_schema: list[dict] = field(default_factory=list)
    ui: dict = field(default_factory=dict)
    dependencies: dict = field(default_factory=dict)
    trust_level: str = "community"          # "official" | "verified" | "community"


# --------------------------------------------------------------------------
# Lifecycle base
# --------------------------------------------------------------------------

class PluginBase(ABC):
    """Every plugin type extends this. `manifest` is filled in by the loader."""

    manifest: PluginManifest

    def __init__(self, context: "PluginContext"):
        self.context = context

    def on_load(self) -> None:
        """Called once, right after the plugin module is imported and instantiated."""

    def on_enable(self) -> None:
        """Called when the plugin transitions to enabled (including at startup)."""

    def on_disable(self) -> None:
        """Called when the plugin is disabled. Must release timers/sockets/etc."""

    def on_unload(self) -> None:
        """Called right before the plugin is uninstalled or the app shuts down."""

    def on_settings_changed(self, settings: dict) -> None:
        """Called after the user saves this plugin's settings panel."""


# --------------------------------------------------------------------------
# Type interfaces
# --------------------------------------------------------------------------

class DownloaderPlugin(PluginBase):
    """A source fnack can fetch audio from. Multiple may be installed; the
    queue tries them in ascending `priority` order until one succeeds."""

    priority: int = 100

    @abstractmethod
    def can_handle(self, track: TrackRef) -> bool:
        """Cheap pre-check — no network calls — before attempting a download."""

    @abstractmethod
    def download(self, track: TrackRef, dest_dir: Path, options: dict) -> DownloadResult:
        ...

    def is_rate_limited(self) -> bool:
        """Queue skips this plugin (without penalizing it) while True."""
        return False


class MetadataProviderPlugin(PluginBase):
    """Artist/album/track metadata lookup, e.g. a Deezer/MusicBrainz-style source."""

    priority: int = 100

    @abstractmethod
    def search_artist(self, name: str) -> list[dict]:
        ...

    @abstractmethod
    def get_artist_discography(self, provider_artist_id: str) -> dict:
        ...

    def get_track_info(self, provider_track_id: str) -> Optional[dict]:
        return None


class FingerprintPlugin(PluginBase):
    """Acoustic identification / verification (e.g. an AcoustID-style plugin)."""

    @abstractmethod
    def identify(self, file_path: Path) -> FingerprintResult:
        ...


class ScanTriggerPlugin(PluginBase):
    """Tells an external media server (Navidrome/Plex/Jellyfin/...) to rescan."""

    @abstractmethod
    def trigger_scan(self) -> tuple[bool, str]:
        ...

    def test_connection(self) -> tuple[bool, str]:
        return False, "test_connection() not implemented"


class RecommendationPlugin(PluginBase):
    @abstractmethod
    def recommend_for_artist(self, artist_id: int) -> list[RecommendationItem]:
        ...

    def recommend_similar_tracks(self, track_id: int) -> list[RecommendationItem]:
        return []


class LibraryTaskPlugin(PluginBase):
    """A maintenance/cleanup job. `schedule=None` means manual-trigger-only."""

    schedule: Optional[str] = None   # e.g. "daily", "hourly", or a cron string

    @abstractmethod
    def run(self) -> TaskResult:
        ...


class VPNPlugin(PluginBase):
    @abstractmethod
    def start(self) -> tuple[bool, str]:
        ...

    @abstractmethod
    def stop(self) -> tuple[bool, str]:
        ...

    @abstractmethod
    def status(self) -> dict:
        ...


class ServerExtensionPlugin(PluginBase):
    """For plugins that need entirely new HTTP routes, e.g. a Subsonic-
    compatible API surface so third-party Subsonic clients can talk to fnack
    directly."""

    @abstractmethod
    def register_routes(self, blueprint: Any) -> None:
        """`blueprint` is a fresh flask.Blueprint scoped to this plugin;
        add routes to it with the normal @blueprint.route(...) decorator."""


class UIExtensionPlugin(PluginBase):
    """Pure UI contribution. Most of the behavior is declarative — see the
    manifest's `ui.slots` — this class exists for plugins that need to
    compute dynamic content for their slot(s)."""

    def render_slot(self, slot_name: str, context_data: dict) -> str:
        """Return an HTML fragment for the given slot. Called by core's
        template helper; never rendered with raw/unescaped user input."""
        return ""


class EventHookPlugin(PluginBase):
    """No required methods — behavior lives entirely in on_load(), via
    self.context.events.subscribe(event_name, callback). Useful for
    notifications, webhooks, and cross-cutting flags (see the
    example-quality-flag demo plugin)."""
