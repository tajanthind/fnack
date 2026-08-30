"""Bundled first-party plugin: MusicBrainz metadata provider (priority 20).

Enrichment-ONLY per wayfinder/tickets/musicbrainz-integration.md: Deezer is
authoritative; MusicBrainz only ever ADDS mb_* facts on confident matches.
The 1 req/s pacing, Retry-After backoff and 30d negative cache live inside
services/musicbrainz_service.py (module state) — this plugin calls through
those same functions so the throttle is preserved verbatim, NOT "one more
provider in the list". `enrich_albums` stays core glue (called from sync/
import orchestration), exposed here as a passthrough for chain consumers.
"""

from typing import Optional

from plugins.base import MetadataProviderPlugin
from services.musicbrainz_service import (
    enrich_albums,
    search_artist_cached,
)


class MusicBrainzProvider(MetadataProviderPlugin):
    priority = 20

    def search_artist(self, name: str) -> list[dict]:
        hit = search_artist_cached(name)
        if not hit:
            return []
        return [{"id": hit.get("id"), "name": hit.get("name", name)}]

    def get_artist_discography(self, provider_artist_id: str) -> dict:
        # MusicBrainz has no full-discography endpoint; enrichment happens via
        # enrich_albums() at sync time. Returning empty signals "nothing to
        # add here" without shadowing the Deezer discography.
        return {"artist_name": "", "albums": []}

    def enrich(self, artist_name: str, albums: list) -> None:
        enrich_albums(artist_name, albums)
