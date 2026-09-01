"""Bundled first-party plugin: Deezer batch metadata provider (priority 10).

Wraps services/deezer_service.py for the BATCH/import enrichment path only.
The interactive /api/search-artist route stays CORE (calls deezer_service
directly) per the user decision — this plugin exists for the queue/import
enrichment chain. Behavior-preserving: same functions, same return shapes.
"""

from typing import Optional

from plugins.base import MetadataProviderPlugin
from services.deezer_service import (
    get_album_info,
    get_artist_discography,
    get_track_info,
    search_artist,
)


class DeezerBatchProvider(MetadataProviderPlugin):
    priority = 10

    def search_artist(self, name: str) -> list[dict]:
        return search_artist(name, limit=10)

    def get_artist_discography(self, provider_artist_id: str, **filters) -> dict:
        """Discography with the caller's filters (filter_remixes/...).
        Accepts **filters so MetadataService can pass them through (the
        service inspects signatures and only forwards accepted kwargs)."""
        return get_artist_discography(int(provider_artist_id), **filters)

    def get_album_info(self, provider_album_id: str) -> Optional[dict]:
        return get_album_info(int(provider_album_id))

    def get_track_info(self, provider_track_id: str) -> Optional[dict]:
        return get_track_info(int(provider_track_id))
