"""Bundled first-party plugin: Spotify metadata/URL-resolution provider (priority 30).

Wraps services/spotify_service.py resolve_spotify_url (ISRC-first, zero-auth)
used by the download pipeline. It is NOT user-facing search (that's Deezer,
core) — it's a pipeline resolver, so it's a plain metadata_provider plugin.
"""

from typing import Optional

from plugins.base import MetadataProviderPlugin
from services.spotify_service import resolve_spotify_url


class SpotifyProvider(MetadataProviderPlugin):
    priority = 30

    def search_artist(self, name: str) -> list[dict]:
        return []

    def get_artist_discography(self, provider_artist_id: str) -> dict:
        return {"artist_name": "", "albums": []}

    def get_track_info(self, provider_track_id: str) -> Optional[dict]:
        return None

    def resolve_track_url(
        self,
        song_name: str,
        artist_name: str,
        album_name: Optional[str] = None,
        isrc: Optional[str] = None,
        track_number: Optional[int] = None,
    ) -> Optional[str]:
        client_id = self.context.settings.get("client_id") or None
        client_secret = self.context.settings.get("client_secret") or None
        return resolve_spotify_url(
            song_name,
            artist_name,
            album_name=album_name,
            isrc=isrc,
            track_number=track_number,
            client_id=client_id,
            client_secret=client_secret,
        )
