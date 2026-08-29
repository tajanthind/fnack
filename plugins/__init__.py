"""fnack plugin framework.

Public surface for plugin authors:

    from plugins.base import (
        PluginBase, DownloaderPlugin, MetadataProviderPlugin, FingerprintPlugin,
        ScanTriggerPlugin, RecommendationPlugin, LibraryTaskPlugin, VPNPlugin,
        ServerExtensionPlugin, UIExtensionPlugin, EventHookPlugin,
        DownloadResult, FingerprintResult, TaskResult, RecommendationItem, TrackRef,
    )

Everything else in this package (`manager`, `registry`, `context`, `events`,
`models`, `api`) is core-side machinery that plugin authors never import
directly.
"""

PLUGIN_API_VERSION = "1.0.0"
