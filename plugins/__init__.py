"""fnack plugin framework.

Public surface for plugin authors:

    from plugins.base import (
        PluginBase, DownloaderPlugin, MetadataProviderPlugin, FingerprintPlugin,
        ScanTriggerPlugin, RecommendationPlugin, LibraryTaskPlugin, VPNPlugin,
        ServerExtensionPlugin, UIExtensionPlugin, EventHookPlugin,
        LyricsProviderPlugin, StorageBackendPlugin, AuthProviderPlugin,
        LibrarySourcePlugin, ConflictResolverPlugin,
        DownloadResult, FingerprintResult, TaskResult, RecommendationItem, TrackRef,
    )

Everything else in this package (`manager`, `registry`, `context`, `events`,
`models`, `api`) is core-side machinery that plugin authors never import
directly.
"""

PLUGIN_API_VERSION = "1.0.0"

# The canonical `plugin.json` `type` enum — a single source of truth for
# valid plugin types. HARNESS §3 types are folded in now (even where the
# implementation ships later) so the manifest schema never has to change
# shape twice. Keep in sync with the type table in docs/plugins/AUTHORING.md
# and the interface classes in plugins/base.py.
VALID_TYPES = frozenset({
    "downloader",
    "metadata_provider",
    "lyrics_provider",
    "fingerprint",
    "scan_trigger",
    "library_task",
    "vpn",
    "storage_backend",
    "server_extension",
    "ui_extension",
    "event_hook",
    "auth_provider",
    "library_source",
    "conflict_resolver",
    "recommendation",
})
