"""A minimal synchronous pub/sub bus shared by core and plugins.

Core code calls `event_bus.emit("track.verified", track_id=..., ...)` at the
relevant points (queue_service, import_service, navidrome_service, ...).
Plugins call `context.events.subscribe("track.verified", my_callback)` in
`on_load()`. Callback exceptions are caught and logged by the caller
(PluginManager wraps every emit in `_call_safe`) so one bad subscriber never
breaks another, or core.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

logger = logging.getLogger("fnack.plugins.events")

# Suggested event names — not enforced, just documented here so plugin
# authors and core both have one place to look.
KNOWN_EVENTS = [
    "track.before_download",
    "track.after_download",
    "track.verified",
    "track.caution_flagged",
    "album.imported",
    "artist.added",
    "artist.synced",
    "library.scan_requested",
    "queue.job_completed",
    "queue.job_failed",
    "maintenance.run",
]


class EventBus:
    """Subscriptions are tagged with the subscribing plugin's id so
    `unsubscribe_all_for()` can clean everything up in one call when a
    plugin is disabled or unloaded — a plugin author never has to remember
    to unsubscribe manually."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[tuple[str, Callable[..., None]]]] = defaultdict(list)

    def subscribe(self, event_name: str, callback: Callable[..., None], plugin_id: str = "") -> None:
        self._subscribers[event_name].append((plugin_id, callback))

    def unsubscribe(self, event_name: str, callback: Callable[..., None]) -> None:
        self._subscribers[event_name] = [
            (pid, cb) for pid, cb in self._subscribers.get(event_name, []) if cb is not callback
        ]

    def unsubscribe_all_for(self, plugin_id: str) -> None:
        """Used by PluginManager on disable/unload to clean up in one call."""
        for event_name, subs in self._subscribers.items():
            self._subscribers[event_name] = [(pid, cb) for pid, cb in subs if pid != plugin_id]

    def emit(self, event_name: str, **payload) -> None:
        for _plugin_id, callback in list(self._subscribers.get(event_name, [])):
            try:
                callback(**payload)
            except Exception:
                logger.exception("Plugin event callback failed for %s", event_name)
