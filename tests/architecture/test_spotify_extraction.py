"""Architecture/parity test: Spotify provider extraction (Phase 4, PR 1).

Verifies the Phase 4 extraction contract for fnack.spotify:

1. The provider implementation lives in the plugin (`spotify.py`), NOT in
   `services/` — `services/spotify_service.py` is deleted and no core file
   imports it.
2. The plugin is AUTHORITATIVE: it owns the implementation, its settings
   (client_id/client_secret), and serves the `track.resolve` capability
   (MetadataService.resolve_track_url resolves it provider-neutrally).
3. Legacy `spotify_client_id` / `spotify_client_secret` AppSettings surface
   is removed from app.py (the plugin owns those settings; on_load migrates
   any previously-persisted values).
4. Documentation describes the post-extraction architecture: README/DEPLOY/
   AUTHORING attribute Spotify functionality to the fnack.spotify plugin and
   the track.resolve capability — never to a core service file.

Run from the repo root:

    .venv/bin/python tests/architecture/test_spotify_extraction.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_provider_impl_lives_in_plugin_not_core() -> None:
    """The Spotify implementation moved into the plugin; the core service is
    gone; no core file imports it."""
    plugin_module = ROOT / "bundled_plugins" / "fnack.spotify" / "spotify.py"
    assert plugin_module.exists(), "plugin-owned spotify.py must exist"
    assert not (ROOT / "services" / "spotify_service.py").exists(), \
        "services/spotify_service.py must be deleted"

    for py in [ROOT / "app.py", *(ROOT / "services").glob("*.py")]:
        text = py.read_text(encoding="utf-8")
        assert "spotify_service" not in text, f"{py.name} still imports the deleted service"


def test_plugin_is_authoritative_and_serves_track_resolve() -> None:
    """The plugin owns the implementation (imports its own spotify.py, not a
    core service) and serves track.resolve."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fnack_plugin_fnack_spotify",
        ROOT / "bundled_plugins" / "fnack.spotify" / "plugin.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    plugin_dir = str(ROOT / "bundled_plugins" / "fnack.spotify")
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

    # The plugin module imports its OWN spotify.py (authoritative), not
    # services.spotify_service.
    import inspect
    src = inspect.getsource(module)
    assert "import spotify" in src
    assert "services.spotify_service" not in src
    cls = module.SpotifyProvider
    # track.resolve capability with resolve_track_url method
    import importlib
    from plugins.base import MetadataProviderPlugin
    assert issubclass(cls, MetadataProviderPlugin)
    assert hasattr(cls, "resolve_track_url")
    assert hasattr(cls, "on_load")
    # Plugin-owned settings migration: on_load maps legacy globals -> plugin
    # settings (the plugin is authoritative).
    on_load_src = inspect.getsource(cls.on_load)
    assert "spotify_client_id" in on_load_src and "client_id" in on_load_src


def test_legacy_settings_surface_removed_from_app() -> None:
    """app.py no longer exposes spotify_client_id/secret — the plugin owns
    those settings."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "spotify_client_id" not in src
    assert "spotify_client_secret" not in src


def test_docs_describe_post_extraction_architecture() -> None:
    """Docs attribute Spotify to the fnack.spotify plugin + track.resolve —
    never to services/spotify_service.py."""
    for doc in [ROOT / "README.md", ROOT / "DEPLOY.md", ROOT / "docs" / "plugins" / "AUTHORING.md"]:
        text = doc.read_text(encoding="utf-8")
        assert "services/spotify_service.py" not in text, f"{doc.name} must not name the deleted service"
    # DEPLOY describes the provider through its plugin + capability.
    deploy = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "fnack.spotify" in deploy and "track.resolve" in deploy
    # AUTHORING says the plugin is AUTHORITATIVE.
    auth = (ROOT / "docs" / "plugins" / "AUTHORING.md").read_text(encoding="utf-8")
    assert "AUTHORITATIVE" in auth


def test_metadata_service_resolves_track_resolve_provider_neutrally() -> None:
    """MetadataService.resolve_track_url resolves track.resolve providers —
    the fnack.spotify plugin serves it; core names no provider."""
    from services.metadata_service import MetadataService
    src = (ROOT / "services" / "metadata_service.py").read_text(encoding="utf-8")
    assert "fnack.spotify" not in src, "MetadataService must not name the provider"
    assert "track.resolve" in src


if __name__ == "__main__":
    test_provider_impl_lives_in_plugin_not_core()
    test_plugin_is_authoritative_and_serves_track_resolve()
    test_legacy_settings_surface_removed_from_app()
    test_docs_describe_post_extraction_architecture()
    test_metadata_service_resolves_track_resolve_provider_neutrally()
    print("test_spotify_extraction: PASSED")
