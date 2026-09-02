"""Bundled first-party plugin: iTunes Search API provider (priority 40, fallback).

AUTHORITATIVE implementation (Phase 4): the iTunes-specific logic — artist
album/track lookups with filter + artist-match verification — lives in this
plugin's `itunes.py` (moved from the deleted `services/itunes_service.py`).
This module serves the artist.search / artist.discography / album.tracks
capabilities; core never imports an iTunes implementation.

`get_artist_discography(provider_artist_id)` accepts an ARTIST NAME (iTunes
is keyed by name for this flow), so iTunes is a GENUINE fallback when the
Deezer provider is disabled or fails (chain: Deezer 10 -> MusicBrainz 20 ->
Spotify 30 -> iTunes 40).
"""

from typing import Optional

from plugins.base import MetadataProviderPlugin

# Multi-file plugin (Phase 2): the manager puts this plugin's dir on sys.path,
# so the sibling provider module imports by name.
import itunes  # noqa: E402


class ITunesProvider(MetadataProviderPlugin):
    priority = 40

    def search_artist(self, name: str) -> list[dict]:
        albums = itunes.get_itunes_artist_albums(name, limit=50)
        return [{"id": str(a.get("collection_id") or a.get("artist_id") or ""), "name": name} for a in albums]

    def get_artist_discography(self, provider_artist_id: str) -> dict:
        """provider_artist_id is the ARTIST NAME (iTunes keyed by name here).

        Returns the discography shape the sync consumes:
        {artist_name, albums: [{id, title, year, cover_url, record_type,
        tracks: []}]}. Tracks are fetched lazily via get_album_tracks by
        callers that need them; the sync creates album rows from this list."""
        artist_name = (provider_artist_id or "").strip()
        if not artist_name:
            return {"artist_name": "", "albums": []}
        albums = itunes.get_itunes_artist_albums(artist_name, limit=100)
        return {
            "artist_name": artist_name,
            "albums": [
                {
                    "id": str(a.get("itunes_id") or a.get("collection_id") or ""),
                    "title": a.get("title") or "",
                    "year": a.get("year"),
                    "cover_url": a.get("cover_url"),
                    "record_type": a.get("record_type", "album"),
                    "tracks": [],
                }
                for a in albums
                if a.get("itunes_id") or a.get("collection_id")
            ],
        }

    def get_track_info(self, provider_track_id: str) -> Optional[dict]:
        return None

    def get_album_tracks(self, collection_id: int) -> list[dict]:
        return itunes.get_itunes_album_tracks(collection_id)
