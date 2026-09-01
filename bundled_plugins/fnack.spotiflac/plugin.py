"""Bundled first-party plugin: SpotiFLAC downloader (priority 10, primary).

Phase 2: authoritative implementation. All provider-specific behavior — CLI
process invocation, Xvfb handling, extension management, retries, the pacing
rate limiter, and the 429 circuit breaker — lives in this plugin's
`spotiflac.py` (moved verbatim from the deleted
`services/spotiflac_service.py`). This module implements the FINAL SDK
`TrackDownloader` contract (request-object based, async) and exposes
`is_rate_limited()` for the queue chain's generic circuit-breaker check.

Settings (plugin-owned, Phase 2 §Provider setting migration):
    quality  -> legacy global spotiflac_quality fallback
    delay    -> legacy global spotiflac_delay fallback
    timeout  -> new (defaults 180s)

The plugin also subscribes to the core `network.route_changed` event (emitted
by vpn_service after a tunnel change) to reset its own 429 circuit breaker —
provider state stays in the plugin; core only emits the event.
"""

from fnack.plugin_api import (
    DOWNLOAD_TRACK,
    DownloadRequest,
    DownloadResult,
)
from fnack.plugin_api.providers import TrackDownloader
from plugins.base import PluginBase

# Multi-file plugin (Phase 2): the manager puts this plugin's dir on
# sys.path, so the sibling provider module imports by name.
import spotiflac  # noqa: E402


class SpotiFLACPlugin(PluginBase, TrackDownloader):
    capability_id = DOWNLOAD_TRACK
    priority = 10

    # -- lifecycle -----------------------------------------------------------

    def on_load(self) -> None:
        # Phase 2 settings migration: plugin setting is authoritative; the
        # legacy global is a one-time fallback that migrates into the plugin
        # store (the global is no longer read afterwards).
        legacy_map = {"quality": "spotiflac_quality", "delay": "spotiflac_delay"}
        for key, legacy_key in legacy_map.items():
            if not self.context.settings.get(key):
                legacy = self.context.library.get_setting(legacy_key, "")
                self.context.settings.set(key, legacy or {"quality": "LOSSLESS", "delay": "3.0"}[key])
        if not self.context.settings.get("timeout"):
            self.context.settings.set("timeout", "180")
        # Provider-owned circuit breaker: reset when core reports a route
        # change (VPN tunnel up/down = fresh IP). Core emits the event; the
        # plugin owns the breaker state.
        self.context.events.subscribe("network.route_changed", self._on_route_changed)

    def _on_route_changed(self, **payload) -> None:
        spotiflac.reset_spotiflac_rate_limit()

    # -- SDK TrackDownloader contract (FINAL, Phase 2) -----------------------

    async def can_handle(self, request: DownloadRequest) -> bool:
        # SpotiFLAC needs a resolvable Spotify URL (ISRC-first resolution
        # happens upstream in the pipeline / manual path).
        return bool(request and request.track and request.track.spotify_url)

    async def download(self, request: DownloadRequest) -> DownloadResult:
        track = request.track
        quality = request.quality or self.context.settings.get("quality") or "LOSSLESS"
        try:
            delay = float(self.context.settings.get("delay") or 3.0)
        except (TypeError, ValueError):
            delay = 3.0
        try:
            timeout = int(self.context.settings.get("timeout") or 180)
        except (TypeError, ValueError):
            timeout = 180

        ok, file, err = spotiflac.download_track_spotiflac(
            track.spotify_url,
            request.destination,
            quality=quality,
            timeout_seconds=timeout,
            rate_limit_delay=delay,
        )
        return DownloadResult(
            provider_id=self.manifest.id,
            success=bool(ok),
            path=file,
            error_code=None,
            message=err,
            retryable=True,
            metadata={"format": file.suffix.lstrip(".") if file else None},
        )

    # -- queue-chain circuit breaker (generic, called by the DownloadService) -

    def is_rate_limited(self) -> bool:
        return spotiflac.is_spotiflac_rate_limited()
