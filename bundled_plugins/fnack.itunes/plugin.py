"""Bundled first-party plugin: iTunes Search API provider (priority 40, fallback)."""

from typing import Optional

from plugins.base import MetadataProviderPlugin
from services.itunes_service import get_itunes_album_tracks, get_itunes_artist_albums


class ITunesProvider(MetadataProviderPlugin):
    priority = 40

    def search_artist(self, name: str) -> list[dict]:
        albums = get_itunes_artist_albums(name, limit=50)
        return [{"id": str(a.get("collection_id") or a.get("artist_id") or ""), "name": name} for a in albums]

    def get_artist_discography(self, provider_artist_id: str) -> dict:
        return {"artist_name": "", "albums": []}

    def get_track_info(self, provider_track_id: str) -> Optional[dict]:
        return None

    def get_album_tracks(self, collection_id: int) -> list[dict]:
        return get_itunes_album_tracks(collection_id)
