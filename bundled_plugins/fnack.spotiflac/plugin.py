"""Bundled first-party plugin: SpotiFLAC downloader (priority 10, primary).

Wraps services/spotiflac_service.py 1:1 — behavior-preserving (PHASE1 §1):
same can_handle/download/is_rate_limited semantics, no logic changes. The
module-level 429 circuit breaker + pacing delay stay process-wide; this
plugin instance is the only caller while enabled.
"""

from pathlib import Path
from typing import Optional

from plugins.base import DownloadResult, DownloaderPlugin, TrackRef
from services.spotiflac_service import (
    download_track_spotiflac,
    is_spotiflac_rate_limited,
)


class SpotiFLACDownloader(DownloaderPlugin):
    priority = 10

    def can_handle(self, track: TrackRef) -> bool:
        # SpotiFLAC needs a resolvable Spotify URL (ISRC-first resolution
        # happens upstream in the queue pipeline).
        return bool(track and track.spotify_url)

    def download(self, track: TrackRef, dest_dir: Path, options: dict) -> DownloadResult:
        quality = options.get("quality") or "LOSSLESS"
        delay = options.get("delay")
        try:
            delay = float(delay) if delay is not None else None
        except (TypeError, ValueError):
            delay = None
        ok, file, err = download_track_spotiflac(
            track.spotify_url,
            dest_dir,
            quality=quality,
            rate_limit_delay=delay,
        )
        return DownloadResult(
            success=bool(ok),
            file_path=file,
            error=err,
            source_plugin_id=self.manifest.id,
            extra={"format": file.suffix.lstrip(".") if file else None},
        )

    def is_rate_limited(self) -> bool:
        return is_spotiflac_rate_limited()
