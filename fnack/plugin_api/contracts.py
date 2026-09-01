"""Capability contracts: the SINGLE mapping from capability ID to the
interface/method a provider must implement (Phase 1.1 §2).

One source of truth — do not duplicate this mapping anywhere. The runtime
(PluginManager) uses it to validate that a manifest-declared capability is
actually implemented by the loaded plugin object; a capability whose
contract methods are missing is skipped (not the whole plugin) with a clear
warning.

## Two contracts, explicitly

- **CURRENT COMPATIBILITY CONTRACT** (this file): what the runtime validates
  today. Method names are the ACTUAL interfaces already established by FNACK
  (plugins/base.py + the bundled official plugins) — the OLD, working
  signatures, e.g. `can_handle(track: TrackRef)`,
  `download(track, dest_dir, options)`, `resolve_track_url(...)`.
- **FINAL SDK CONTRACT** (fnack/plugin_api/providers.py): the aspirational
  capability protocols — e.g. `TrackDownloader.can_handle(request:
  DownloadRequest)`, `TrackResolver.resolve(request: TrackResolveRequest)`.
  They intentionally differ (request objects instead of positional args).

These two MUST NOT be treated as the same thing. Phase 2 inserts a migration
adapter layer:

    CURRENT COMPATIBILITY CONTRACT (this file)
            ↓ Phase 2 migration adapter
    FINAL SDK CONTRACT (providers.py)

Until that adapter lands, the runtime validates and invokes the CURRENT
contract, and the SDK protocols are documentation of the target shape — do
NOT rewrite existing plugins to the new protocol signatures as a
"Phase 1.1 cleanup" (that is Phase 2 work, done one provider at a time).

## Method names (CURRENT contract)

    download.track        -> DownloaderPlugin.can_handle / download
    track.resolve         -> Spotify plugin's resolve_track_url
    track.metadata        -> MetadataProviderPlugin.get_track_info
    artist.search         -> MetadataProviderPlugin.search_artist
    artist.discography    -> MetadataProviderPlugin.get_artist_discography
    album.metadata        -> (no plugin implements get_album_info yet — the
                             contract is checked so a false declaration is
                             rejected rather than crashing later)
    fingerprint.identify  -> FingerprintPlugin.identify
    media.scan            -> ScanTriggerPlugin.trigger_scan
    media.health          -> (no established method yet; checked so a false
                             declaration is rejected)
    media.connection_test -> ScanTriggerPlugin.test_connection
    library.task          -> LibraryTaskPlugin.run
    server.extension      -> ServerExtensionPlugin.register_routes
    auth.provider         -> AuthProviderPlugin.authenticate
    notification.event    -> EventHookPlugin (subscribes in on_load)
    network.route         -> VPNPlugin.start / stop / status
"""

from __future__ import annotations

from fnack.plugin_api.capabilities import (
    ALBUM_METADATA,
    ARTIST_DISCOGRAPHY,
    ARTIST_SEARCH,
    AUTH_PROVIDER,
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
)

# capability_id -> required method names (all must exist on the instance).
CAPABILITY_METHODS: dict[str, tuple[str, ...]] = {
    DOWNLOAD_TRACK: ("can_handle", "download"),
    TRACK_RESOLVE: ("resolve_track_url",),
    TRACK_METADATA: ("get_track_info",),
    ARTIST_SEARCH: ("search_artist",),
    ARTIST_DISCOGRAPHY: ("get_artist_discography",),
    ALBUM_METADATA: ("get_album_info",),
    FINGERPRINT_IDENTIFY: ("identify",),
    MEDIA_SCAN: ("trigger_scan",),
    MEDIA_HEALTH: ("health",),
    MEDIA_CONNECTION_TEST: ("test_connection",),
    LIBRARY_TASK: ("run",),
    SERVER_EXTENSION: ("register_routes",),
    AUTH_PROVIDER: ("authenticate",),
    NETWORK_ROUTE: ("start", "stop", "status"),
    # notification.event: EventHookPlugin has no required methods — it
    # subscribes in on_load. Any plugin can declare it, but the capability is
    # only meaningful for EventHookPlugin instances; validation checks the
    # base class rather than a method.
}

# capability_id -> plugins.base class name the instance must be an instance
# of (in addition to the method check). Empty means no base-class check.
CAPABILITY_BASE_CLASS: dict[str, str] = {
    NOTIFICATION_EVENT: "EventHookPlugin",
}


def validate_capability_contract(
    plugin_id: str,
    capability_id: str,
    instance: object,
) -> list[str]:
    """Return the list of MISSING requirements for `capability_id` on
    `instance` ([] means the capability is validly implemented).

    The returned strings are human-readable and contain the plugin ID,
    capability ID, and expected interface — suitable for a clear warning
    instead of a cryptic AttributeError later at invocation time."""
    missing: list[str] = []
    for method_name in CAPABILITY_METHODS.get(capability_id, ()):
        if not callable(getattr(instance, method_name, None)):
            missing.append(f"method '{method_name}'")
    base_name = CAPABILITY_BASE_CLASS.get(capability_id, "")
    if base_name:
        from plugins import base as _base
        base_cls = getattr(_base, base_name, None)
        if base_cls is not None and not isinstance(instance, base_cls):
            missing.append(f"must be a {base_name} instance")
    return missing
