"""Bundled first-party plugin: Deezer batch metadata provider (priority 10).

AUTHORITATIVE implementation (Phase 4): the Deezer-specific logic — public
API access with retry pooling, artist search, artist-info lookup,
rate-limited discography ingestion with filters, track/album metadata — lives
in this plugin's `deezer.py` (moved from the deleted
`services/deezer_service.py`). This module serves the artist.search /
artist.discography / track.metadata / album.metadata / artist.info
capabilities through the plugin boundary; core never imports a Deezer
implementation.

The interactive /api/search-artist route and onboarding /api/add-artist route
go through MetadataService, which resolves the capabilities this plugin
serves (priority 10, authoritative).
"""

from typing import Optional

from plugins.base import MetadataProviderPlugin

# Multi-file plugin (Phase 2): the manager puts this plugin's dir on sys.path,
# so the sibling provider module imports by name.
import deezer  # noqa: E402


class DeezerBatchProvider(MetadataProviderPlugin):
    priority = 10

    def search_artist(self, name: str) -> list[dict]:
        return deezer.search_artist(name, limit=10)

    def get_artist_info(self, provider_artist_id: str) -> dict:
        return deezer.get_artist_info(int(provider_artist_id))

    def get_artist_discography(self, provider_artist_id: str, **filters) -> dict:
        """Discography with the caller's filters (filter_remixes/...).
        Accepts **filters so MetadataService can pass them through (the
        service inspects signatures and only forwards accepted kwargs)."""
        return deezer.get_artist_discography(int(provider_artist_id), **filters)

    def get_album_info(self, provider_album_id: str) -> Optional[dict]:
        return deezer.get_album_info(int(provider_album_id))

    def get_track_info(self, provider_track_id: str) -> Optional[dict]:
        return deezer.get_track_info(int(provider_track_id))

    def search_album(self, query: str, limit: int = 20) -> list[dict]:
        return deezer.search_album(query, limit=limit)

    def search_track(self, query: str, limit: int = 20) -> list[dict]:
        return deezer.search_track(query, limit=limit)

    def get_album_tracks(self, provider_album_id: str) -> list[dict]:
        return deezer.get_album_tracks(int(provider_album_id))
