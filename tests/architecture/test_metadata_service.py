"""Architecture/parity test: MetadataService (Phase 3, Step 2).

Verifies the Phase 3 application-service contract for metadata:

1. MetadataService resolves each metadata capability via the capability
   registry (priority order) — no services.spotify/deezer/musicbrainz/itunes
   imports and no provider-ID branches in the service.
2. Zero enabled providers -> CapabilityUnavailable(capability, operation)
   for every method.
3. search_artist / resolve_track_url / get_track_metadata /
   get_album_metadata: first non-empty provider result wins.
4. get_artist_discography: first provider with albums wins; filters are
   forwarded only to providers that accept them (signature inspection).
5. Callers migrated: app.py and import_service use MetadataService (no
   direct services.deezer_service imports); queue_service no longer imports
   deezer/spotify services.
6. The tag-normalization module moved to services/tag_normalization_service.py
   (the brief mandates services/metadata_service.py for the capability-based
   service).

Run from the repo root:

    .venv/bin/python tests/architecture/test_metadata_service.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _fake_manager(*providers):
    """Minimal manager stand-in: get_capability_providers returns the
    providers; has_capability reflects non-empty; invoke_provider consumes
    timeout and calls the method directly."""
    import asyncio
    import inspect

    class _Cap:
        def __init__(self, capability_id, provider):
            self.capability_id = capability_id
            self.provider = provider

    from collections import defaultdict
    caps = defaultdict(list)
    for p in providers:
        if getattr(p, "capability_id", None):
            caps[p.capability_id].append(p)

    class _FakeManager:
        def has_capability(self, capability):
            return capability in caps and bool(caps[capability])

        def get_capability_providers(self, capability):
            if capability not in caps or not caps[capability]:
                raise Exception("no providers")
            return list(caps[capability])

        def invoke_provider(self, provider, method_name, *args, timeout=None, **kwargs):
            method = getattr(provider, method_name, None)
            if method is None:
                return None
            r = method(*args, **kwargs)
            if inspect.isawaitable(r):
                return asyncio.run(r)
            return r

    return _FakeManager()


class _Provider:
    """Metadata provider stub with an optional capability_id."""

    def __init__(self, capability_id=None):
        self.capability_id = capability_id

    @property
    def manifest(self):
        class _M:
            id = "fnack.test"
            name = "Test"
        return _M()

    def search_artist(self, name):
        return []

    def get_artist_discography(self, provider_artist_id, **filters):
        return {"artist_name": "", "albums": []}

    def get_track_info(self, provider_track_id):
        return None

    def get_album_info(self, provider_album_id):
        return None

    def resolve_track_url(self, song_name, artist_name, album_name=None,
                          isrc=None, track_number=None):
        return None


def test_service_has_no_provider_imports_or_id_branches() -> None:
    """Source-level: MetadataService imports only the SDK; it never names a
    provider and never branches on provider IDs."""
    src = (ROOT / "services" / "metadata_service.py").read_text(encoding="utf-8")
    for needle in ["services.spotify_service", "services.deezer_service",
                   "services.musicbrainz_service", "services.itunes_service",
                   '"fnack.spotify"', '"fnack.deezer-batch"',
                   '"fnack.musicbrainz"', '"fnack.itunes"',
                   "provider.manifest.id =="]:
        assert needle not in src, f"MetadataService must stay provider-neutral ({needle})"
    for cap in ["track.resolve", "artist.search", "artist.discography",
                "track.metadata", "album.metadata"]:
        assert cap in src, f"MetadataService must resolve {cap}"


def test_zero_providers_raise_capability_unavailable() -> None:
    """Zero enabled providers -> structured CapabilityUnavailable for every
    method (no hidden fallback)."""
    from services.metadata_service import CapabilityUnavailable, MetadataService

    svc = MetadataService(manager=_fake_manager())
    cases = [
        (lambda: svc.resolve_track_url("T", "A"), "track.resolve"),
        (lambda: svc.search_artist("A"), "artist.search"),
        (lambda: svc.get_artist_discography("1"), "artist.discography"),
        (lambda: svc.get_track_metadata("1"), "track.metadata"),
        (lambda: svc.get_album_metadata("1"), "album.metadata"),
    ]
    for call, cap in cases:
        try:
            call()
        except CapabilityUnavailable as e:
            assert e.capability == cap, f"expected {cap}, got {e.capability}"
        else:
            raise AssertionError(f"expected CapabilityUnavailable for {cap}")


def test_search_artist_first_non_empty_wins() -> None:
    """search_artist tries providers in order; first non-empty wins and is
    capped to limit."""
    from services.metadata_service import MetadataService

    empty = _Provider("artist.search")
    full = _Provider("artist.search")
    full.search_artist = lambda name: [{"id": i, "name": f"{name} {i}"} for i in range(15)]
    svc = MetadataService(manager=_fake_manager(empty, full))
    results = svc.search_artist("Queen", limit=8)
    assert len(results) == 8
    assert results[0]["name"] == "Queen 0"


def test_resolve_track_url_first_non_empty_wins() -> None:
    from services.metadata_service import MetadataService

    empty = _Provider("track.resolve")
    full = _Provider("track.resolve")
    full.resolve_track_url = lambda *a, **k: "https://open.spotify.com/track/x"
    svc = MetadataService(manager=_fake_manager(empty, full))
    url = svc.resolve_track_url("T", "A", isrc="QZ123")
    assert url == "https://open.spotify.com/track/x"


def test_get_track_and_album_metadata_first_non_empty_wins() -> None:
    from services.metadata_service import MetadataService

    empty = _Provider("track.metadata")
    full = _Provider("track.metadata")
    full.get_track_info = lambda tid: {"isrc": "QZ123", "genre": "Rock"}
    svc = MetadataService(manager=_fake_manager(empty, full))
    assert svc.get_track_metadata("7") == {"isrc": "QZ123", "genre": "Rock"}

    e2 = _Provider("album.metadata")
    f2 = _Provider("album.metadata")
    f2.get_album_info = lambda aid: {"title": "A Night at the Opera"}
    svc2 = MetadataService(manager=_fake_manager(e2, f2))
    assert svc2.get_album_metadata("9") == {"title": "A Night at the Opera"}


def test_discography_filters_forwarded_only_when_accepted() -> None:
    """get_artist_discography passes filters only to providers that accept
    them; the first provider with albums wins."""
    from services.metadata_service import MetadataService

    class WithFilters:
        capability_id = "artist.discography"
        @property
        def manifest(self):
            class _M:
                id = "fnack.with-filters"
                name = "WithFilters"
            return _M()
        def get_artist_discography(self, provider_artist_id, **filters):
            assert "filter_remixes" in filters, "filters must be forwarded"
            assert "artist_name" in filters, "artist_name must be forwarded"
            return {"artist_name": "X", "albums": [{"id": provider_artist_id}]}

    # provider that ignores filters first -> still works (filters not passed)
    class NoKwargs:
        capability_id = "artist.discography"
        @property
        def manifest(self):
            class _M:
                id = "fnack.no-kwargs"
                name = "NoKwargs"
            return _M()
        def get_artist_discography(self, provider_artist_id):
            return {"artist_name": "Z", "albums": [{"id": "nokwargs"}]}

    # WithFilters first (priority) -> wins with filters forwarded
    svc = MetadataService(manager=_fake_manager(WithFilters(), NoKwargs()))
    d = svc.get_artist_discography("42", artist_name="Queen",
                                   filter_remixes=False, include_singles=True)
    assert d["albums"][0]["id"] == "42"
    assert d["artist_name"] == "X"

    # NoKwargs first -> filters not passed, still works
    svc2 = MetadataService(manager=_fake_manager(NoKwargs(), WithFilters()))
    d2 = svc2.get_artist_discography("7", artist_name="Queen", filter_remixes=False)
    assert d2["albums"][0]["id"] == "nokwargs"


def test_callers_migrated_to_application_service() -> None:
    """app.py / import_service / queue_service no longer import the deezer or
    spotify provider services directly for metadata (deezer_service in app.py
    remains only for get_artist_info onboarding, which has no capability)."""
    app_src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "MetadataService" in app_src
    assert "services.spotify_service" not in app_src
    # deezer_service import must be scoped (get_artist_info only)
    assert "from services.deezer_service import" not in app_src

    imp_src = (ROOT / "services" / "import_service.py").read_text(encoding="utf-8")
    assert "MetadataService" in imp_src
    assert "from services.deezer_service import" not in imp_src
    assert "from services.spotify_service import" not in imp_src

    queue_src = (ROOT / "services" / "queue_service.py").read_text(encoding="utf-8")
    assert "MetadataService" in queue_src
    assert "from services.deezer_service import" not in queue_src
    assert "from services.spotify_service import" not in queue_src


def test_tag_normalization_module_renamed() -> None:
    """The brief mandates services/metadata_service.py for the capability
    service; the old tag-normalization module lives at
    services/tag_normalization_service.py now."""
    assert (ROOT / "services" / "tag_normalization_service.py").exists()
    src = (ROOT / "services" / "tag_normalization_service.py").read_text(encoding="utf-8")
    assert "normalize_album_tags" in src
    # Scripts import from the renamed module
    s1 = (ROOT / "scripts" / "normalize_album_tags.py").read_text(encoding="utf-8")
    s2 = (ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8")
    assert "tag_normalization_service" in s1 and "tag_normalization_service" in s2


if __name__ == "__main__":
    test_service_has_no_provider_imports_or_id_branches()
    test_zero_providers_raise_capability_unavailable()
    test_search_artist_first_non_empty_wins()
    test_resolve_track_url_first_non_empty_wins()
    test_get_track_and_album_metadata_first_non_empty_wins()
    test_discography_filters_forwarded_only_when_accepted()
    test_callers_migrated_to_application_service()
    test_tag_normalization_module_renamed()
    print("test_metadata_service: PASSED")
