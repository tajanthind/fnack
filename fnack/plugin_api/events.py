"""Event names + the event bus, re-exported for plugin authors.

Same classes the runtime uses (`plugins.events`); this is the stable SDK
import path.
"""

from __future__ import annotations

from plugins.events import KNOWN_EVENTS, EventBus  # noqa: F401
