"""fnack plugin public SDK (`fnack.plugin_api`).

The stable boundary plugin authors and application services code against.
Plugins import from here (or `plugins.base` for the concrete base classes —
kept for backward compatibility); they never import `app.py`, `models.py`,
or private `services/*`.

Public surface:

    from fnack.plugin_api import (
        # capability IDs + registry
        DOWNLOAD_TRACK, TRACK_RESOLVE, ..., CapabilityRegistry, ProviderHandle,
        # domain models
        TrackRef, DownloadRequest, DownloadResult, FingerprintEvidence, ...,
        # provider protocols + executor
        TrackDownloader, FingerprintProvider, ProviderExecutor,
        # errors
        PluginError, CapabilityUnavailable, ProviderError,
        # context + events
        PluginContext, EventBus,
        # version
        SDK_VERSION,
    )
"""

from __future__ import annotations

from fnack.plugin_api.capabilities import (  # noqa: F401
    ALBUM_METADATA,
    ALL_CAPABILITIES,
    ARTIST_DISCOGRAPHY,
    ARTIST_SEARCH,
    AUTH_PROVIDER,
    DOWNLOAD_BATCH,
    DOWNLOAD_TRACK,
    FINGERPRINT_IDENTIFY,
    LIBRARY_TASK,
    MEDIA_CONNECTION_TEST,
    MEDIA_HEALTH,
    MEDIA_SCAN,
    NETWORK_ROUTE,
    NOTIFICATION_EVENT,
    SERVER_EXTENSION,
    TRACK_METADATA,
    TRACK_RESOLVE,
    CapabilityRegistry,
    ProviderHandle,
)
from fnack.plugin_api.errors import (  # noqa: F401
    CapabilityUnavailable,
    PluginError,
    ProviderError,
)
from fnack.plugin_api.models import (  # noqa: F401
    DownloadRequest,
    DownloadResult,
    FingerprintEvidence,
    FingerprintRequest,
    FingerprintResult,
    RecommendationItem,
    TaskResult,
    TrackCandidate,
    TrackRef,
    TrackResolveRequest,
)
from fnack.plugin_api.providers import (  # noqa: F401
    AuthProvider,
    FingerprintProvider,
    LibraryTaskProvider,
    MediaScanner,
    NetworkRouter,
    NotificationProvider,
    ProviderExecutor,
    ServerExtension,
    TrackDownloader,
    TrackResolver,
)
from fnack.plugin_api.version import SDK_VERSION  # noqa: F401

# Context + events re-exported so `from fnack.plugin_api import PluginContext`
# works without reaching into the private package.
from fnack.plugin_api.context import (  # noqa: F401
    EventsContext,
    FSContext,
    JobsContext,
    LibraryContext,
    PluginContext,
    SettingsContext,
    UIContext,
)
from fnack.plugin_api.events import KNOWN_EVENTS, EventBus  # noqa: F401

__all__ = [
    # capabilities
    "ALBUM_METADATA", "ALL_CAPABILITIES", "ARTIST_DISCOGRAPHY", "ARTIST_SEARCH",
    "AUTH_PROVIDER", "DOWNLOAD_BATCH", "DOWNLOAD_TRACK", "FINGERPRINT_IDENTIFY",
    "LIBRARY_TASK", "MEDIA_CONNECTION_TEST", "MEDIA_HEALTH", "MEDIA_SCAN",
    "NETWORK_ROUTE", "NOTIFICATION_EVENT", "SERVER_EXTENSION", "TRACK_METADATA",
    "TRACK_RESOLVE", "CapabilityRegistry", "ProviderHandle",
    # errors
    "CapabilityUnavailable", "PluginError", "ProviderError",
    # models
    "DownloadRequest", "DownloadResult", "FingerprintEvidence",
    "FingerprintRequest", "FingerprintResult", "RecommendationItem",
    "TaskResult", "TrackCandidate", "TrackRef", "TrackResolveRequest",
    # providers
    "AuthProvider", "FingerprintProvider", "LibraryTaskProvider",
    "MediaScanner", "NetworkRouter", "NotificationProvider",
    "ProviderExecutor", "ServerExtension", "TrackDownloader", "TrackResolver",
    # context + events
    "EventsContext", "FSContext", "JobsContext", "LibraryContext",
    "PluginContext", "SettingsContext", "UIContext",
    "KNOWN_EVENTS", "EventBus",
    # version
    "SDK_VERSION",
]
