"""Architecture/parity test: provider-neutral identity (final cleanup).

Proves core artist/metadata workflows operate on opaque provider-neutral
external identities and never assume a Deezer (or integer) id:

A. A fake provider serving artist.search with a NON-integer, non-Deezer
   external id ("fake-artist-123") flows through MetadataService untouched.
B. A second fake provider whose identity format differs ("artist:abc:def")
   is treated as opaque provider identity too.
C. The fnack.deezer-batch plugin converts the provider-neutral input into
   the Deezer-specific representation it needs (int parsing) INSIDE the
   plugin; foreign ids are declined gracefully (None), never crash.
D. Removing the Deezer provider: with only the fake provider(s) enabled the
   capability chain still works; with NO provider the service raises
   structured CapabilityUnavailable — no hidden Deezer fallback.

Run from the repo root:

    .venv/bin/python tests/architecture/test_provider_neutral_identity.py
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _fake_manager(*providers):
    """Minimal manager stand-in (same shape as test_metadata_service)."""
    import asyncio
    import inspect
    from collections import defaultdict

    caps = defaultdict(list)
    for p in providers:
        if getattr(p, "capability_id", None):
            caps[p.capability_id].append(p)

    class _FakeManager:
        def has_capability(self, capability):
            return capability in caps and bool(caps[capability])

        def get_capability_providers(self, capability):
            return list(caps.get(capability, []))

        def invoke_provider(self, provider, method_name, *args, timeout=None, **kwargs):
            method = getattr(provider, method_name, None)
            if method is None:
                return None
            r = method(*args, **kwargs)
            if inspect.isawaitable(r):
                return asyncio.run(r)
            return r

    return _FakeManager()


class _IdentityProvider:
    """Fake metadata provider returning results keyed by an opaque external
    id (the id is never parsed or interpreted by the harness)."""

    def __init__(self, capability_id, external_id):
        self.capability_id = capability_id
        self._external_id = external_id

    @property
    def manifest(self):
        class _M:
            id = "com.example.fake"
            name = "Fake"
        return _M()

    def search_artist(self, name):
        # Provider-specific identity: opaque string, arbitrary format.
        return [{"id": self._external_id, "name": name}]

    def get_artist_discography(self, provider_artist_id, **filters):
        if provider_artist_id != self._external_id:
            return {"artist_name": "", "albums": []}
        return {"artist_name": "Fake Artist",
                "albums": [{"id": "fake-album-9", "title": "Fake Album",
                            "tracks": [{"id": "fake-track-7", "title": "Fake Song",
                                        "duration": 200.0, "isrc": "FAKE1"}]}]}


def _service(*providers):
    from services.metadata_service import MetadataService
    return MetadataService(manager=_fake_manager(*providers))


def test_a_generic_non_integer_artist_identity_flows_through() -> None:
    """Test A: an artist.search result with external id 'fake-artist-123'
    (non-integer, non-Deezer) is returned by core without conversion."""
    svc = _service(_IdentityProvider("artist.search", "fake-artist-123"),
                   _IdentityProvider("artist.discography", "fake-artist-123"))
    results = svc.search_artist("Someone")
    assert results and results[0]["id"] == "fake-artist-123", results
    assert results[0]["name"] == "Someone"
    # The identity round-trips into the discography capability unchanged.
    disco = svc.get_artist_discography("fake-artist-123", artist_name="Someone")
    assert disco["albums"][0]["id"] == "fake-album-9"


def test_b_alternate_provider_identity_format_is_opaque() -> None:
    """Test B: a second provider's identity format ('artist:abc:def') is
    treated as opaque — core never parses, int()s, or prefixes it."""
    svc = _service(_IdentityProvider("artist.search", "artist:abc:def"),
                   _IdentityProvider("artist.discography", "artist:abc:def"))
    results = svc.search_artist("X")
    assert results and results[0]["id"] == "artist:abc:def", results
    # The matching opaque identity round-trips into the discography capability.
    disco = svc.get_artist_discography("artist:abc:def", artist_name="X")
    assert disco["albums"][0]["id"] == "fake-album-9"
    # An id from ANOTHER provider returns the structured empty shape — the
    # provider declines it; never an exception or a hidden fallback.
    disco2 = svc.get_artist_discography("some-other-provider-id")
    assert disco2 == {"artist_name": "", "albums": []}


def test_c_deezer_plugin_owns_id_conversion() -> None:
    """Test C: the Deezer plugin converts the provider-neutral input into the
    Deezer-specific int representation inside the plugin; non-Deezer ids are
    declined (None) — core never does this conversion."""
    plugin_dir = ROOT / "bundled_plugins" / "fnack.deezer-batch"
    sys.path.insert(0, str(plugin_dir))   # plugin.py imports its sibling `deezer`
    try:
        mod_path = plugin_dir / "plugin.py"
        spec = importlib.util.spec_from_file_location("fnack_deezer_batch_plugin_under_test", mod_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(plugin_dir))

    seen = []
    # Numeric Deezer id: parsed to the int the Deezer API requires.
    result = mod.DeezerBatchProvider._deezer_id_lookup(
        "27", lambda i: seen.append(i) or ("ok", i))
    assert seen == [27] and result == ("ok", 27), (seen, result)
    # Foreign / non-numeric id: gracefully declined, NOT parsed as Deezer.
    seen2 = []
    declined = mod.DeezerBatchProvider._deezer_id_lookup(
        "fake-artist-123", lambda i: seen2.append(i) or ("ok", i))
    assert declined is None and seen2 == [], (declined, seen2)


def test_d_provider_removal_and_zero_providers() -> None:
    """Test D: without the Deezer provider, core still works when another
    provider implements the capability; with NO provider it raises
    CapabilityUnavailable — no hidden Deezer fallback anywhere."""
    from services.metadata_service import CapabilityUnavailable

    fake = _IdentityProvider("artist.search", "fake-artist-123")
    fake_disco = _IdentityProvider("artist.discography", "fake-artist-123")
    svc = _service(fake, fake_disco)
    assert svc.search_artist("Someone")[0]["id"] == "fake-artist-123"
    assert svc.get_artist_discography("fake-artist-123")["albums"][0]["id"] == "fake-album-9"

    # No providers at all -> structured CapabilityUnavailable per capability.
    empty = _service()
    for call, cap in [
        (lambda: empty.search_artist("A"), "artist.search"),
        (lambda: empty.get_artist_discography("1"), "artist.discography"),
        (lambda: empty.resolve_track_url("T", "A"), "track.resolve"),
    ]:
        try:
            call()
        except CapabilityUnavailable as e:
            assert e.capability == cap, f"expected {cap}, got {e.capability}"
        else:
            raise AssertionError(f"expected CapabilityUnavailable for {cap}")

    # Core source never falls back to a direct Deezer call.
    import re
    for path in [ROOT / "services" / "metadata_service.py",
                 ROOT / "services" / "queue_service.py",
                 ROOT / "app.py"]:
        src = path.read_text(encoding="utf-8")
        assert not re.search(r"services\.deezer_service|import deezer\b", src), path


if __name__ == "__main__":
    test_a_generic_non_integer_artist_identity_flows_through()
    test_b_alternate_provider_identity_format_is_opaque()
    test_c_deezer_plugin_owns_id_conversion()
    test_d_provider_removal_and_zero_providers()
    print("test_provider_neutral_identity: PASSED")
