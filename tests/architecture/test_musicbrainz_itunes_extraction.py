"""Architecture/parity test: MusicBrainz + iTunes provider extraction (Phase 4, PR 3).

Verifies the Phase 4 extraction contract for fnack.musicbrainz and
fnack.itunes:

1. Both implementations live in their plugins (musicbrainz.py / itunes.py),
   NOT in services/ — the core services are deleted, no core file imports
   them.
2. Both plugins are AUTHORITATIVE: they import their own modules and serve
   their capabilities (musicbrainz: artist.search + enrich; itunes:
   artist.search / artist.discography / album.tracks).
3. MusicBrainz provider cache is plugin-owned (in-memory), not a core DB
   model — the plugin imports no core models.
4. Sync/import enrichment routes through the plugin chain with NO hidden
   fallback to services.musicbrainz_service.
5. Docs describe the post-extraction architecture.

Run from the repo root:

    .venv/bin/python tests/architecture/test_musicbrainz_itunes_extraction.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_impls_live_in_plugins_not_core() -> None:
    """Both implementations moved into their plugins; core services gone; no
    core file imports them."""
    assert (ROOT / "bundled_plugins" / "fnack.musicbrainz" / "musicbrainz.py").exists()
    assert (ROOT / "bundled_plugins" / "fnack.itunes" / "itunes.py").exists()
    assert not (ROOT / "services" / "musicbrainz_service.py").exists()
    assert not (ROOT / "services" / "itunes_service.py").exists()

    for py in [ROOT / "app.py", ROOT / "services" / "import_service.py",
               ROOT / "services" / "queue_service.py"]:
        text = py.read_text(encoding="utf-8")
        assert "services.musicbrainz_service" not in text
        assert "services.itunes_service" not in text


def test_plugins_are_authoritative() -> None:
    """Each plugin imports its own module (not a core service) and exposes its
    capability methods."""
    import importlib.util

    for plugin_id, methods in [("fnack.musicbrainz", ["search_artist", "enrich"]),
                               ("fnack.itunes", ["search_artist", "get_artist_discography", "get_album_tracks"])]:
        spec = importlib.util.spec_from_file_location(
            f"fnack_plugin_{plugin_id.replace('.', '_')}",
            ROOT / "bundled_plugins" / plugin_id / "plugin.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        plugin_dir = str(ROOT / "bundled_plugins" / plugin_id)
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
        # imports its own module by name (multi-file pattern)
        own = "musicbrainz" if plugin_id == "fnack.musicbrainz" else "itunes"
        assert f"import {own}" in src
        assert "services." not in src, f"{plugin_id} must not import services"
        cls = next(v for k, v in module.__dict__.items() if k.endswith("Provider"))
        for m in methods:
            assert hasattr(cls, m), f"{plugin_id} must expose {m}"


def test_musicbrainz_plugin_owns_its_cache() -> None:
    """The MusicBrainz provider cache is plugin-owned (in-memory module state)
    — the plugin imports no core DB models."""
    src = (ROOT / "bundled_plugins" / "fnack.musicbrainz" / "musicbrainz.py").read_text(encoding="utf-8")
    assert "from models import" not in src, "plugin must not import core models"
    assert "MusicBrainzCache" not in src, "core cache model must be gone"
    assert "_cache:" in src, "plugin must own its cache state"
    assert "db.session" not in src, "plugin must not touch the core session for its cache"


def test_enrichment_has_no_hidden_fallback() -> None:
    """app.py / import_service route enrichment through the plugin chain; no
    services.musicbrainz_service fallback remains."""
    app_src = (ROOT / "app.py").read_text(encoding="utf-8")
    imp_src = (ROOT / "services" / "import_service.py").read_text(encoding="utf-8")
    assert "services.musicbrainz_service" not in app_src
    assert "services.musicbrainz_service" not in imp_src
    # The chain is invoked via the manager boundary (enrich provider method).
    assert "invoke_provider(provider, \"enrich\"" in app_src
    assert "invoke_provider(provider, \"enrich\"" in imp_src


def test_docs_describe_post_extraction_architecture() -> None:
    """Docs attribute MusicBrainz/iTunes to the plugins — never to the
    deleted services. The deep architecture doc names the plugins; the
    README (user-facing, plugin-ID-free) must not present them as core."""
    for doc in [ROOT / "README.md", ROOT / "DEPLOY.md", ROOT / "docs" / "plugins" / "AUTHORING.md"]:
        text = doc.read_text(encoding="utf-8")
        assert "services/musicbrainz_service.py" not in text
        assert "services/itunes_service.py" not in text
    arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "fnack.itunes" in arch and "fnack.musicbrainz" in arch
    assert "fnack.itunes" not in (ROOT / "README.md").read_text(encoding="utf-8")


if __name__ == "__main__":
    test_impls_live_in_plugins_not_core()
    test_plugins_are_authoritative()
    test_musicbrainz_plugin_owns_its_cache()
    test_enrichment_has_no_hidden_fallback()
    test_docs_describe_post_extraction_architecture()
    print("test_musicbrainz_itunes_extraction: PASSED")
