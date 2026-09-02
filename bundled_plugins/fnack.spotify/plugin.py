"""Bundled first-party plugin: Spotify track-URL resolution provider (priority 30).

AUTHORITATIVE implementation (Phase 4): the Spotify-specific logic — zero-auth
ISRC-first URL resolution, title verification, album parsing, rate limiting,
and the optional client-id/secret API path — lives in this plugin's
`spotify.py` (moved from the deleted `services/spotify_service.py`). This
module implements the `track.resolve` capability through the plugin boundary;
core never imports a Spotify implementation.

It is NOT user-facing search (that's the Deezer provider) — it is a pipeline
resolver: MetadataService.resolve_track_url() resolves `track.resolve`
providers in priority order and this plugin serves the Spotify URL.
"""

from typing import Optional

from plugins.base import MetadataProviderPlugin

# Multi-file plugin (Phase 2): the manager puts this plugin's dir on sys.path,
# so the sibling provider module imports by name.
import spotify  # noqa: E402


class SpotifyProvider(MetadataProviderPlugin):
    priority = 30

    def on_load(self) -> None:
        # Settings migration (Phase 3): plugin settings are authoritative;
        # legacy globals are a one-time fallback migrated into the plugin
        # store (removed with the legacy settings in the Phase 4 deletion).
        legacy_map = {
            "client_id": ("spotify_client_id", ""),
            "client_secret": ("spotify_client_secret", ""),
        }
        for key, (legacy_key, default) in legacy_map.items():
            if not self.context.settings.get(key):
                legacy = self.context.library.get_setting(legacy_key, "")
                self.context.settings.set(key, legacy or default)

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
        return spotify.resolve_spotify_url(
            song_name,
            artist_name,
            album_name=album_name,
            isrc=isrc,
            track_number=track_number,
            client_id=client_id,
            client_secret=client_secret,
        )
