"""MetadataService — application service for the metadata capabilities.

Phase 3 (brief 03): the queue/app/import orchestrate; this service owns
metadata resolution. Each method resolves ONE capability through the
capability registry (enabled, priority-ordered providers) and invokes the
best provider through the manager's ProviderExecutor boundary. Core never
imports a provider implementation and never names a provider; providers are
selected by capability and registry priority alone.

Methods (each resolves a capability):

- resolve_track_url(...)            -> track.resolve
- search_artist(...)                -> artist.search
- get_artist_discography(...)       -> artist.discography
- get_track_metadata(...)           -> track.metadata
- get_album_metadata(...)           -> album.metadata
- get_artist_info(...)              -> artist.info
- search_album(...)                 -> album.search
- search_track(...)                 -> track.search
- get_album_tracks(...)             -> album.tracks

Policy (provider resolution per MASTER §Provider resolution):

- metadata providers may be MERGED; this service applies the current
  behavior-preserving policy: try providers in priority order and return the
  first non-empty result. Zero enabled providers -> CapabilityUnavailable
  (a valid state — MASTER rule 3); no hidden provider fallback.
"""

from __future__ import annotations

from typing import Optional

from fnack.plugin_api.capabilities import (
    ALBUM_METADATA,
    ALBUM_SEARCH,
    ALBUM_TRACKS,
    ARTIST_DISCOGRAPHY,
    ARTIST_INFO,
    TRACK_SEARCH,
    ARTIST_SEARCH,
    TRACK_METADATA,
    TRACK_RESOLVE,
)
from fnack.plugin_api.errors import CapabilityUnavailable



def _provider_id_of(provider) -> Optional[str]:
    """Plugin id of a provider (provenance data for persisted identities)."""
    manifest = getattr(provider, "manifest", None)
    pid = getattr(manifest, "id", None)
    return str(pid) if pid else None


class MetadataService:
    """Owns metadata capability resolution + the first-non-empty policy."""

    def __init__(self, manager=None):
        self._manager = manager  # injectable for tests

    # -- resolution ---------------------------------------------------------

    def _pm(self):
        if self._manager is not None:
            return self._manager
        from plugins.manager import plugin_manager
        return plugin_manager

    def _providers_for(self, capability: str, operation: str) -> list:
        """Enabled providers for a capability, priority-ordered. Raises
        CapabilityUnavailable (this service's operation name) when no enabled
        plugin provides it."""
        pm = self._pm()
        if pm is None:
            raise CapabilityUnavailable(capability, operation, "plugin manager not ready")
        if not pm.has_capability(capability):
            raise CapabilityUnavailable(capability, operation)
        return pm.get_capability_providers(capability)

    def _invoke(self, provider, method_name, *args, **kwargs):
        """Invoke through the manager's guarded boundary (executor + timeout
        + health/auto-disable) — never a raw call."""
        pm = self._pm()
        if pm is None:
            return None
        return pm.invoke_provider(provider, method_name, *args, **kwargs)

    # -- track.resolve ------------------------------------------------------

    def resolve_track_url(
        self,
        song_name: str,
        artist_name: str,
        album_name: Optional[str] = None,
        isrc: Optional[str] = None,
        track_number: Optional[int] = None,
    ) -> Optional[str]:
        """Resolve a track URL (e.g. Spotify) for the download pipeline.
        First provider returning a non-empty URL wins. CapabilityUnavailable
        when no track.resolve provider is enabled."""
        providers = self._providers_for(TRACK_RESOLVE, "resolve_track_url")
        for provider in providers:
            try:
                url = self._invoke(
                    provider, "resolve_track_url",
                    song_name, artist_name,
                    album_name=album_name, isrc=isrc, track_number=track_number,
                )
            except Exception:
                continue
            if url:
                return url
        return None

    # -- artist.search ------------------------------------------------------

    def search_artist(self, name: str, limit: int = 10) -> list[dict]:
        """Search artists by name. First provider returning results wins
        (providers are tried in registry priority order — the ordering is
        configuration/registry policy, not core). CapabilityUnavailable when
        no artist.search provider is enabled.

        Each result is annotated with the ``provider`` that supplied it (the
        winning provider's plugin id) so callers can persist provider-scoped
        identities; the annotation is data, never a branch."""
        providers = self._providers_for(ARTIST_SEARCH, "search_artist")
        for provider in providers:
            try:
                found = self._invoke(provider, "search_artist", name) or []
            except Exception:
                continue
            if found:
                provider_id = _provider_id_of(provider)
                return [dict(item, provider=provider_id) if isinstance(item, dict) else item
                        for item in found][:limit]
        return []

    # -- artist.discography -------------------------------------------------

    def get_artist_discography(
        self,
        provider_artist_id: str,
        *,
        artist_name: Optional[str] = None,
        **filters,
    ) -> dict:
        """Fetch an artist's discography (see get_artist_discography_with_provider)."""
        disco, _ = self.get_artist_discography_with_provider(
            provider_artist_id, artist_name=artist_name, **filters)
        return disco

    def get_artist_discography_with_provider(
        self,
        provider_artist_id: str,
        *,
        artist_name: Optional[str] = None,
        **filters,
    ) -> tuple:
        """Fetch an artist's discography; returns ``(discography, provider_id)``
        where provider_id is the plugin id of the provider that SERVED it
        (None when none could). First provider returning a usable discography
        wins. ``provider_artist_id`` is the opaque external identity the
        selected provider understands — the provider owns parsing/conversion;
        core never interprets it. ``artist_name`` is passed to providers keyed
        by name. The service is provider-neutral and does not branch on
        provider IDs.

        ``filters`` (filter_remixes/filter_lofi/...) are passed to providers
        that accept them; providers that don't ignore them. Provider-specific
        filter semantics live in the provider. CapabilityUnavailable when no
        artist.discography provider is enabled.
        """
        providers = self._providers_for(ARTIST_DISCOGRAPHY, "get_artist_discography")
        for provider in providers:
            try:
                accepts = _accepts_keywords(provider, "get_artist_discography")
                wildcard = "**kwargs" in accepts
                kwargs = {}
                if artist_name and (wildcard or "artist_name" in accepts):
                    kwargs["artist_name"] = artist_name
                for k, v in (filters or {}).items():
                    if wildcard or k in accepts:
                        kwargs[k] = v
                d = self._invoke(provider, "get_artist_discography",
                                 provider_artist_id, **kwargs)
            except Exception:
                continue
            if d and d.get("albums"):
                return d, _provider_id_of(provider)
        return {"artist_name": artist_name or "", "albums": []}, None

    # -- artist.info --------------------------------------------------------

    def get_artist_info(self, provider_artist_id: str) -> Optional[dict]:
        """Basic artist details (name/image/etc.) by provider artist id.
        First provider returning a non-empty dict wins. CapabilityUnavailable
        when no artist.info provider is enabled."""
        providers = self._providers_for(ARTIST_INFO, "get_artist_info")
        for provider in providers:
            try:
                info = self._invoke(provider, "get_artist_info", provider_artist_id)
            except Exception:
                continue
            if info:
                return info
        return None

    # -- album.search / track.search / album.tracks (Lidarr-facing) ---------

    def search_album(self, query: str, limit: int = 20) -> list[dict]:
        """Live album search. First provider returning results wins.
        CapabilityUnavailable when no album.search provider is enabled."""
        providers = self._providers_for(ALBUM_SEARCH, "search_album")
        for provider in providers:
            try:
                found = self._invoke(provider, "search_album", query, limit=limit) or []
            except Exception:
                continue
            if found:
                return found
        return []

    def search_track(self, query: str, limit: int = 20) -> list[dict]:
        """Live track search. First provider returning results wins.
        CapabilityUnavailable when no track.search provider is enabled."""
        providers = self._providers_for(TRACK_SEARCH, "search_track")
        for provider in providers:
            try:
                found = self._invoke(provider, "search_track", query, limit=limit) or []
            except Exception:
                continue
            if found:
                return found
        return []

    def get_album_tracks(self, provider_album_id: str) -> list[dict]:
        """Tracks for an album. First provider returning results wins.
        CapabilityUnavailable when no album.tracks provider is enabled."""
        providers = self._providers_for(ALBUM_TRACKS, "get_album_tracks")
        for provider in providers:
            try:
                tracks = self._invoke(provider, "get_album_tracks", provider_album_id) or []
            except Exception:
                continue
            if tracks:
                return tracks
        return []

    # -- track.metadata -----------------------------------------------------

    def get_track_metadata(self, provider_track_id: str) -> Optional[dict]:
        """Track metadata (ISRC/genre/etc.) by provider track id. First
        provider returning a non-empty dict wins. CapabilityUnavailable when
        no track.metadata provider is enabled."""
        providers = self._providers_for(TRACK_METADATA, "get_track_metadata")
        for provider in providers:
            try:
                meta = self._invoke(provider, "get_track_info", provider_track_id)
            except Exception:
                continue
            if meta:
                return meta
        return None

    # -- album.metadata -----------------------------------------------------

    def get_album_metadata(self, provider_album_id: str) -> Optional[dict]:
        """Album metadata by provider album id. First provider returning a
        non-empty dict wins. CapabilityUnavailable when no album.metadata
        provider is enabled."""
        providers = self._providers_for(ALBUM_METADATA, "get_album_metadata")
        for provider in providers:
            try:
                meta = self._invoke(provider, "get_album_info", provider_album_id)
            except Exception:
                continue
            if meta:
                return meta
        return None


def _accepts_keywords(provider, method_name: str) -> set:
    """Keyword args the provider's method accepts (signature inspection)."""
    import inspect
    method = getattr(provider, method_name, None)
    if method is None:
        return set()
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return set()
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return {"**kwargs"}
    return {p for p, pv in params.items()
            if pv.kind in (inspect.Parameter.KEYWORD_ONLY,
                           inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and p != "self"}
