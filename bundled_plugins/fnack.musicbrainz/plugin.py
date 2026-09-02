"""Bundled first-party plugin: MusicBrainz metadata provider (priority 20).

AUTHORITATIVE implementation (Phase 4): the MusicBrainz-specific logic —
catalogue enrichment, the 1 req/s pacing + Retry-After backoff, and the
found/not-found/error cache — lives in this plugin's `musicbrainz.py` (moved
from the deleted `services/musicbrainz_service.py`; the legacy core DB cache
model is replaced by plugin-owned in-memory state). This module serves the
artist.search capability and exposes `enrich` for sync/import enrichment;
core never imports a MusicBrainz implementation.

Enrichment-ONLY: Deezer is authoritative; MusicBrainz only ADDS mb_* facts on
confident matches.
"""

from typing import Optional

from plugins.base import MetadataProviderPlugin

# Multi-file plugin (Phase 2): the manager puts this plugin's dir on sys.path,
# so the sibling provider module imports by name.
import musicbrainz  # noqa: E402


class MusicBrainzProvider(MetadataProviderPlugin):
    priority = 20

    def search_artist(self, name: str) -> list[dict]:
        hit = musicbrainz.search_artist_cached(name)
        if not hit:
            return []
        return [{"id": hit.get("id"), "name": hit.get("name", name)}]

    def get_artist_discography(self, provider_artist_id: str) -> dict:
        # MusicBrainz has no full-discography endpoint; enrichment happens via
        # enrich_albums() at sync time. Returning empty signals "nothing to
        # add here" without shadowing the Deezer discography.
        return {"artist_name": "", "albums": []}

    def enrich(self, artist_name: str, albums: list) -> None:
        musicbrainz.enrich_albums(artist_name, albums)
