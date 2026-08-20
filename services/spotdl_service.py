"""spotdl compatibility layer: Forwarding all requests to ytdlp_service."""

from pathlib import Path
from typing import Optional, Tuple
from services.ytdlp_service import download_track_ytdlp


def download_track_spotdl(
    query_or_url: str,
    output_dir: Path,
    output_format: str = "flac",
    audio_source: str = "youtube",
    timeout_seconds: int = 180,
) -> Tuple[bool, Optional[Path], Optional[str]]:
    """Backward-compatible wrapper routing to fast yt-dlp service."""
    return download_track_ytdlp(
        query_or_url=query_or_url,
        output_dir=output_dir,
        output_format=output_format,
        timeout_seconds=timeout_seconds,
    )
