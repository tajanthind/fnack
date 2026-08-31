"""Capability constants + the CapabilityRegistry.

Capability IDs are provider-neutral contracts (MASTER §Capability IDs):
core depends on `download.track`, not on `fnack.spotiflac`. A plugin may
provide many capabilities; a capability may be served by many providers.

The registry is *separate* from PluginManager: the manager handles
discovery/install/enable/disable/lifecycle/settings/health, the registry
handles capability registration, lookup, ordering, and availability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Sequence

# -- capability IDs ---------------------------------------------------------

DOWNLOAD_TRACK = "download.track"
DOWNLOAD_BATCH = "download.batch"

TRACK_RESOLVE = "track.resolve"
TRACK_METADATA = "track.metadata"

ARTIST_SEARCH = "artist.search"
ARTIST_DISCOGRAPHY = "artist.discography"
ALBUM_METADATA = "album.metadata"

FINGERPRINT_IDENTIFY = "fingerprint.identify"

MEDIA_SCAN = "media.scan"
MEDIA_HEALTH = "media.health"
MEDIA_CONNECTION_TEST = "media.connection_test"

LIBRARY_TASK = "library.task"
SERVER_EXTENSION = "server.extension"
AUTH_PROVIDER = "auth.provider"
NOTIFICATION_EVENT = "notification.event"
NETWORK_ROUTE = "network.route"

ALL_CAPABILITIES: FrozenSet[str] = frozenset({
    DOWNLOAD_TRACK,
    DOWNLOAD_BATCH,
    TRACK_RESOLVE,
    TRACK_METADATA,
    ARTIST_SEARCH,
    ARTIST_DISCOGRAPHY,
    ALBUM_METADATA,
    FINGERPRINT_IDENTIFY,
    MEDIA_SCAN,
    MEDIA_HEALTH,
    MEDIA_CONNECTION_TEST,
    LIBRARY_TASK,
    SERVER_EXTENSION,
    AUTH_PROVIDER,
    NOTIFICATION_EVENT,
    NETWORK_ROUTE,
})


# -- registry ---------------------------------------------------------------

@dataclass(frozen=True)
class ProviderHandle:
    """One provider instance serving one-or-more capabilities.

    `provider` is the plugin instance (or a provider object it exposes).
    `priority` is the plugin's effective priority (manifest priority, or the
    user's priority_override when set) — LOWER numbers are tried FIRST,
    matching fnack's existing downloader/metadata ordering semantics.
    """

    plugin_id: str
    provider: object
    priority: int
    capabilities: FrozenSet[str] = field(default_factory=frozenset)


class CapabilityRegistry:
    """Holds provider registrations for every capability.

    Thread-safe enough for fnack's single-worker gevent model (register/
    unregister happen on plugin enable/disable; lookups are read-only after
    boot). Application services ask the registry "who can do X" and get
    priority-ordered handles — they never name a provider.
    """

    def __init__(self) -> None:
        # plugin_id -> ProviderHandle (one handle per plugin instance; it may
        # serve many capabilities).
        self._handles: dict[str, ProviderHandle] = {}

    def register(
        self,
        plugin_id: str,
        provider: object,
        capabilities: Sequence[str],
        priority: Optional[int] = None,
    ) -> None:
        """Register (or replace) a provider for the given capabilities.

        `priority` defaults to the provider's `priority` class attribute
        (falling back to 100), so plugins that already declare `priority`
        keep their ordering without repeating it here.
        """
        caps = frozenset(capabilities)
        if priority is None:
            priority = int(getattr(provider, "priority", 100) or 100)
        self._handles[plugin_id] = ProviderHandle(
            plugin_id=plugin_id,
            provider=provider,
            priority=int(priority),
            capabilities=caps,
        )

    def providers(self, capability: str) -> list[ProviderHandle]:
        """All enabled providers of `capability`, lowest priority number
        first (stable tie-break by plugin_id)."""
        return sorted(
            (h for h in self._handles.values() if capability in h.capabilities),
            key=lambda h: (h.priority, h.plugin_id),
        )

    def has(self, capability: str) -> bool:
        return any(capability in h.capabilities for h in self._handles.values())

    def unregister_plugin(self, plugin_id: str) -> None:
        self._handles.pop(plugin_id, None)

    def registered_plugins(self) -> list[str]:
        return sorted(self._handles.keys())

    def capabilities_for(self, plugin_id: str) -> list[str]:
        handle = self._handles.get(plugin_id)
        if handle is None:
            return []
        return sorted(handle.capabilities)
