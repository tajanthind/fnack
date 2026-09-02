"""Architecture/parity test: Deezer provider extraction (Phase 4, PR 2).

Verifies the Phase 4 extraction contract for fnack.deezer-batch:

1. The provider implementation lives in the plugin (`deezer.py`), NOT in
   `services/` — `services/deezer_service.py` is deleted and no core file
   imports it.
2. The plugin is AUTHORITATIVE: it owns the implementation and serves the
   artist.search / artist.discography / artist.info / track.metadata /
   album.metadata / album.search / track.search / album.tracks capabilities.
3. app.py's `api_add_artist` (get_artist_info) routes through MetadataService
   (artist.info capability) — fixing the latent NameError from Phase 3.
4. plugins/context.py facade + scripts/reverify_library.py use
   MetadataService, not services.deezer_service.
5. New capabilities (artist.info / album.search / track.search /
   album.tracks) are declared in the SDK and served by the plugin.
6. Docs describe the post-extraction architecture.

Run from the repo root:

    .venv/bin/python tests/architecture/test_deezer_extraction.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_provider_impl_lives_in_plugin_not_core() -> None:
    """The Deezer implementation moved into the plugin; the core service is
    gone; no core file imports it."""
    plugin_module = ROOT / "bundled_plugins" / "fnack.deezer-batch" / "deezer.py"
    assert plugin_module.exists(), "plugin-owned deezer.py must exist"
    assert not (ROOT / "services" / "deezer_service.py").exists(), \
        "services/deezer_service.py must be deleted"

    for py in [ROOT / "app.py", ROOT / "services" / "import_service.py",
               ROOT / "services" / "queue_service.py", ROOT / "plugins" / "context.py"]:
        text = py.read_text(encoding="utf-8")
        assert "from services.deezer_service import" not in text, \
            f"{py.name} still imports the deleted service"


def test_plugin_is_authoritative_and_serves_capabilities() -> None:
    """The plugin imports its own deezer.py (not a core service) and declares
    the full capability set."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fnack_plugin_fnack_deezer",
        ROOT / "bundled_plugins" / "fnack.deezer-batch" / "plugin.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    plugin_dir = str(ROOT / "bundled_plugins" / "fnack.deezer-batch")
    added = plugin_dir not in sys.path
    if added:
        sys.path.insert(0, plugin_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        if added:
            try:
                sys.path.remove(plugin_dir)
            except ValueError:
                pass

    import inspect
    src = inspect.getsource(module)
    assert "import deezer" in src
    assert "services.deezer_service" not in src
    cls = module.DeezerBatchProvider
    for method in ("search_artist", "get_artist_info", "get_artist_discography",
                   "get_album_info", "get_track_info", "search_album",
                   "search_track", "get_album_tracks"):
        assert hasattr(cls, method), f"plugin must expose {method}"

    import json
    manifest = json.load(open(ROOT / "bundled_plugins" / "fnack.deezer-batch" / "plugin.json"))
    for cap in ("artist.search", "artist.discography", "artist.info",
                "track.metadata", "album.metadata", "album.search",
                "track.search", "album.tracks"):
        assert cap in manifest["capabilities"], f"manifest must declare {cap}"


def test_new_capabilities_declared_in_sdk() -> None:
    """artist.info / album.search / track.search / album.tracks exist in the
    SDK capability + contract modules."""
    caps_src = (ROOT / "fnack" / "plugin_api" / "capabilities.py").read_text(encoding="utf-8")
    contracts_src = (ROOT / "fnack" / "plugin_api" / "contracts.py").read_text(encoding="utf-8")
    for const, cap in [("ARTIST_INFO", "artist.info"), ("ALBUM_SEARCH", "album.search"),
                       ("TRACK_SEARCH", "track.search"), ("ALBUM_TRACKS", "album.tracks")]:
        assert const in caps_src and cap in caps_src
        assert const in contracts_src


def test_app_get_artist_info_routes_through_metadata_service() -> None:
    """app.py's api_add_artist resolves artist.info via MetadataService (this
    also fixes the latent NameError from Phase 3 where the deezer import was
    removed but the call remained)."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "MetadataService().get_artist_info" in src
    assert "services.deezer_service" not in src


def test_context_facade_and_script_use_metadata_service() -> None:
    """plugins/context.py and scripts/reverify_library.py resolve Deezer
    metadata through MetadataService — no services.deezer_service import."""
    ctx = (ROOT / "plugins" / "context.py").read_text(encoding="utf-8")
    assert "services.deezer_service" not in ctx
    assert "MetadataService" in ctx
    for method in ("search_album", "search_track", "get_album_metadata",
                   "get_track_metadata", "get_album_tracks"):
        assert method in ctx, f"facade must route {method} through the service"

    rev = (ROOT / "scripts" / "reverify_library.py").read_text(encoding="utf-8")
    assert "services.deezer_service" not in rev
    assert "MetadataService" in rev


def test_metadata_service_resolves_deezer_capabilities() -> None:
    """MetadataService has the Deezer-facing methods and never names the
    provider."""
    src = (ROOT / "services" / "metadata_service.py").read_text(encoding="utf-8")
    assert "fnack.deezer-batch" not in src
    for method in ("get_artist_info", "search_album", "search_track", "get_album_tracks"):
        assert f"def {method}" in src


def test_docs_describe_post_extraction_architecture() -> None:
    """Docs attribute Deezer to the fnack.deezer-batch plugin + capabilities —
    never to services/deezer_service.py."""
    for doc in [ROOT / "README.md", ROOT / "DEPLOY.md", ROOT / "docs" / "plugins" / "AUTHORING.md"]:
        text = doc.read_text(encoding="utf-8")
        assert "services/deezer_service.py" not in text, f"{doc.name} must not name the deleted service"
    deploy = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "fnack.deezer-batch" in deploy


if __name__ == "__main__":
    test_provider_impl_lives_in_plugin_not_core()
    test_plugin_is_authoritative_and_serves_capabilities()
    test_new_capabilities_declared_in_sdk()
    test_app_get_artist_info_routes_through_metadata_service()
    test_context_facade_and_script_use_metadata_service()
    test_metadata_service_resolves_deezer_capabilities()
    test_docs_describe_post_extraction_architecture()
    print("test_deezer_extraction: PASSED")
