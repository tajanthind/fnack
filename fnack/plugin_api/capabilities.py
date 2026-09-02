"""Capability constants + the CapabilityRegistry.

Capability IDs are provider-neutral contracts (MASTER §Capability IDs):
core depends on `download.track`, not on `fnack.spotiflac`. A plugin may
provide many capabilities; a capability may be served by many providers.

The registry is *separate* from PluginManager: the manager handles
discovery/install/enable/disable/lifecycle/settings/health, the registry
handles capability registration, lookup, ordering, and availability.

Priority semantics (Phase 1.1, preserved from fnack's existing chain):
LOWER numeric priority = tried FIRST. Ordering is deterministic:
(priority, plugin_id) ascending — never installation or dict-insertion
order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Mapping, Optional, Sequence

# -- capability IDs ---------------------------------------------------------

DOWNLOAD_TRACK = "download.track"
DOWNLOAD_BATCH = "download.batch"

TRACK_RESOLVE = "track.resolve"
TRACK_METADATA = "track.metadata"

ARTIST_SEARCH = "artist.search"
ARTIST_DISCOGRAPHY = "artist.discography"
ARTIST_INFO = "artist.info"
ALBUM_METADATA = "album.metadata"
ALBUM_SEARCH = "album.search"
TRACK_SEARCH = "track.search"
ALBUM_TRACKS = "album.tracks"

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
    ARTIST_INFO,
    ALBUM_METADATA,
    ALBUM_SEARCH,
    TRACK_SEARCH,
    ALBUM_TRACKS,
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
    `priority` is the plugin-level default priority (manifest priority, or
    the user's plugin-level priority_override when set). `priorities` maps
    capability_id -> effective priority for THAT capability — it may differ
    per capability (Phase 1.1). LOWER numbers are tried FIRST.
    """

    plugin_id: str
    provider: object
    priority: int
    capabilities: FrozenSet[str] = field(default_factory=frozenset)
    priorities: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityProvider:
    """A resolved, capability-specific provider record (Phase 1.1):
    `providers_for(capability)` returns these in deterministic order."""

    plugin_id: str
    capability_id: str
    provider: object
    priority: int  # effective priority FOR THIS capability


class CapabilityRegistry:
    """Holds provider registrations for every capability.

    Thread-safe enough for fnack's single-worker gevent model (register/
    unregister happen on plugin enable/disable; lookups are read-only after
    boot). Application services ask the registry "who can do X" and get
    priority-ordered providers — they never name a provider.
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
        priorities: Optional[Mapping[str, int]] = None,
    ) -> None:
        """Register (or replace) a provider for the given capabilities.

        `priority` defaults to the provider's `priority` class attribute
        (falling back to 100), so plugins that already declare `priority`
        keep their ordering without repeating it here.

        `priorities` optionally overrides priority PER capability
        (Phase 1.1) — a multi-capability plugin can serve `track.resolve`
        at priority 5 and `track.metadata` at priority 30. A capability
        without an entry falls back to `priority`.
        """
        caps = frozenset(capabilities)
        if priority is None:
            priority = int(getattr(provider, "priority", 100) or 100)
        self._handles[plugin_id] = ProviderHandle(
            plugin_id=plugin_id,
            provider=provider,
            priority=int(priority),
            capabilities=caps,
            priorities=dict(priorities or {}),
        )

    def _capability_priority(self, handle: ProviderHandle, capability: str) -> int:
        """Effective priority of `capability` for this handle:
        capability-specific override > plugin-level default."""
        return int(handle.priorities.get(capability, handle.priority))

    def providers(self, capability: str) -> list[ProviderHandle]:
        """All enabled providers of `capability`, lowest effective priority
        number first (stable tie-break by plugin_id)."""
        return sorted(
            (h for h in self._handles.values() if capability in h.capabilities),
            key=lambda h: (self._capability_priority(h, capability), h.plugin_id),
        )

    def providers_for(self, capability: str) -> list[CapabilityProvider]:
        """Capability-specific provider records in deterministic priority
        order (Phase 1.1): lowest priority number first, then plugin_id."""
        return [
            CapabilityProvider(
                plugin_id=h.plugin_id,
                capability_id=capability,
                provider=h.provider,
                priority=self._capability_priority(h, capability),
            )
            for h in self.providers(capability)
        ]

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

    def priority_for(self, plugin_id: str, capability: str) -> Optional[int]:
        """Effective priority of one (plugin, capability) pair, or None if
        the plugin isn't registered for that capability."""
        handle = self._handles.get(plugin_id)
        if handle is None or capability not in handle.capabilities:
            return None
        return self._capability_priority(handle, capability)
