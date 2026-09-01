"""Bundled first-party plugin: yt-dlp downloader (priority 50, fallback).

Phase 2: authoritative implementation. All yt-dlp-specific behavior — CLI
invocation, candidate scoring, YouTube Music preference, cookies handling,
format selection, and yt-dlp-specific errors — lives in this plugin's
`ytdlp.py` (moved verbatim from the deleted `services/ytdlp_service.py`).
This module implements the FINAL SDK `TrackDownloader` contract (request-
object based, async) and exposes the cookies helpers so the core settings
UI can show upload/status through the manager boundary (never a direct
service import).

Per the user decision (wayfinder ticket plugin-spotdl-form.md), the legacy
`spotdl` entry point was an alias of this engine; the alias is now migration
metadata only (no separate fnack.spotdl manifest row, no core spotdl shim).

Settings (plugin-owned, Phase 2 §Provider setting migration):
    format        -> legacy ytdlp_format / spotdl_format fallback
    audio_source  -> legacy spotdl_source fallback
    cookies_file  -> per-plugin uploaded copy (Brief 4 §3)
    cookies_path  -> legacy youtube_cookies_path fallback
    timeout       -> new (defaults 180s)
"""

from pathlib import Path
from typing import Optional

from fnack.plugin_api import (
    DOWNLOAD_TRACK,
    DownloadRequest,
    DownloadResult,
)
from fnack.plugin_api.providers import TrackDownloader
from plugins.base import PluginBase

# Multi-file plugin (Phase 2): the manager puts this plugin's dir on
# sys.path, so the sibling provider module imports by name.
import ytdlp  # noqa: E402


class YtDlpDownloader(PluginBase, TrackDownloader):
    capability_id = DOWNLOAD_TRACK
    priority = 50

    # -- lifecycle -----------------------------------------------------------

    def on_load(self) -> None:
        # Phase 2 settings migration: plugin setting is authoritative; legacy
        # globals are a one-time fallback migrated into the plugin store.
        legacy_map = {
            "format": ("ytdlp_format", "opus"),
            "audio_source": ("spotdl_source", "youtube_music"),
            "cookies_path": ("youtube_cookies_path", "/config/cookies.txt"),
        }
        for key, (legacy_key, default) in legacy_map.items():
            if not self.context.settings.get(key):
                legacy = self.context.library.get_setting(legacy_key, "")
                self.context.settings.set(key, legacy or default)
        if not self.context.settings.get("timeout"):
            self.context.settings.set("timeout", "180")

    # -- SDK TrackDownloader contract (FINAL, Phase 2) -----------------------

    async def can_handle(self, request: DownloadRequest) -> bool:
        # Universal fallback: handles any track (search by title/artist) or
        # a raw query/URL from the manual-download path.
        return bool(request and (request.track or request.query))

    async def download(self, request: DownloadRequest) -> DownloadResult:
        output_format = request.format or self.context.settings.get("format") or "opus"
        audio_source = request.audio_source or self.context.settings.get("audio_source") or "youtube_music"
        cookies_path = request.cookies_path or self.context.settings.get("cookies_path") or ""
        try:
            timeout = int(self.context.settings.get("timeout") or 180)
        except (TypeError, ValueError):
            timeout = 180

        # Manual path passes a raw query/URL; the queue chain searches by
        # title/artist from the track.
        if request.query:
            query = request.query
        else:
            query = f"{request.track.artist_name} - {request.track.title}"

        # Defensive: the SDK contract types destination as Path, but string
        # paths from any caller must not crash the plugin (ytdlp.py does
        # output_dir.mkdir()).
        destination = Path(request.destination)

        ok, file, err = ytdlp.download_track_ytdlp(
            query,
            destination,
            output_format=output_format,
            artist_name=request.track.artist_name,
            track_title=request.track.title,
            expected_duration=request.track.duration,
            cookies_path=cookies_path,
            prefer_youtube_music=(audio_source != "youtube"),
            timeout_seconds=timeout,
            check_duration=request.check_duration,
            # Generic core helpers via the context facade — the plugin never
            # imports services.* (Phase 2 SDK boundary).
            verify_audio_file=self.context.library.verify_audio_file,
            verify_download=self.context.library.verify_download_acoustid,
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

    # -- cookies helpers (settings UI routes through the manager boundary) ----

    def get_cookies_status(self, custom_path: Optional[str] = None) -> dict:
        return ytdlp.get_cookies_status(custom_path)

    def get_cookies_path(self, custom_path: Optional[str] = None) -> Optional[Path]:
        return ytdlp.get_cookies_path(custom_path)
