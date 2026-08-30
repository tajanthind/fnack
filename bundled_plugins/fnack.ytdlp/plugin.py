"""Bundled first-party plugin: yt-dlp downloader (priority 50, fallback).

Wraps services/ytdlp_service.py 1:1. Per the user decision (wayfinder
ticket plugin-spotdl-form.md), the legacy `spotdl_service` shim is an ALIAS
of this plugin — its only function forwards 1:1 to download_track_ytdlp, so
no separate fnack.spotdl manifest row exists. Both entry shapes resolve to
the same underlying function.
"""

from pathlib import Path
from typing import Optional

from plugins.base import DownloadResult, DownloaderPlugin, TrackRef
from services.ytdlp_service import download_track_ytdlp


class YtDlpDownloader(DownloaderPlugin):
    priority = 50

    def can_handle(self, track: TrackRef) -> bool:
        # Universal fallback: handles any track (search by title/artist).
        return bool(track and track.title)

    def download(self, track: TrackRef, dest_dir: Path, options: dict) -> DownloadResult:
        output_format = options.get("format") or "opus"
        # The legacy spotdl default was flac + youtube; yt-dlp's own default is
        # opus + youtube_music. options carries whichever entry point called us.
        ok, file, err = download_track_ytdlp(
            f"{track.artist_name} - {track.title}",
            dest_dir,
            output_format=output_format,
            artist_name=track.artist_name,
            track_title=track.title,
            expected_duration=track.duration,
            prefer_youtube_music=(options.get("audio_source", "youtube_music") != "youtube"),
        )
        return DownloadResult(
            success=bool(ok),
            file_path=file,
            error=err,
            source_plugin_id=self.manifest.id,
            extra={"format": file.suffix.lstrip(".") if file else None},
        )
