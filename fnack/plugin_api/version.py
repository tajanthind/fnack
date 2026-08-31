"""SDK version.

Mirrors the runtime `PLUGIN_API_VERSION` (the `api_version` value core
accepts in manifests). Bump the major when the public SDK contract breaks.
"""

from __future__ import annotations

from plugins import PLUGIN_API_VERSION

SDK_VERSION = PLUGIN_API_VERSION
