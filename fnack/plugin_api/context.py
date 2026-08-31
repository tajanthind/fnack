"""Public context facade — re-export of the runtime PluginContext.

Plugins are handed `self.context` (a PluginContext) at construction; this is
the SDK-visible alias so authors can import it from one stable place:
`fnack.plugin_api.context` instead of the private `plugins.context`.
"""

from __future__ import annotations

from plugins.context import (  # noqa: F401
    EventsContext,
    FSContext,
    JobsContext,
    LibraryContext,
    PluginContext,
    SettingsContext,
    UIContext,
)
