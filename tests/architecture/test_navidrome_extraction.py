"""Architecture/parity test: Navidrome provider extraction (Phase 4, PR 5).

Verifies the Phase 4 extraction contract for fnack.navidrome:

1. The provider implementation lives in the plugin (`navidrome.py`), NOT in
   `services/` — `services/navidrome_service.py` is deleted and no core file
   imports it.
2. The plugin is AUTHORITATIVE: it owns the implementation AND its config
   (url/user/token/auto_scan/db_path via the standard settings schema,
   injected into the module — no core AppSetting reads).
3. The plugin serves media.scan / media.health / media.connection_test and
   exposes run_split_repair (the split-album library task).
4. app.py fix-splits route + scripts resolve through the plugin; no hidden
   fallback to services.navidrome_service.
5. Docs describe the post-extraction architecture.

Run from the repo root:

    .venv/bin/python tests/architecture/test_navidrome_extraction.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_provider_impl_lives_in_plugin_not_core() -> None:
    """The Navidrome implementation moved into the plugin; the core service is
    gone; no core file imports it."""
    plugin_module = ROOT / "bundled_plugins" / "fnack.navidrome" / "navidrome.py"
    assert plugin_module.exists(), "plugin-owned navidrome.py must exist"
    assert not (ROOT / "services" / "navidrome_service.py").exists(), \
        "services/navidrome_service.py must be deleted"

    for py in [ROOT / "app.py", ROOT / "services" / "tag_normalization_service.py",
               ROOT / "services" / "queue_service.py"]:
        text = py.read_text(encoding="utf-8")
        assert "services.navidrome_service" not in text, \
            f"{py.name} still imports the deleted service"


def test_plugin_is_authoritative_and_owns_config() -> None:
    """The plugin imports its own navidrome.py, owns its settings (no core
    AppSetting reads), and exposes the full surface."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fnack_plugin_fnack_navidrome",
        ROOT / "bundled_plugins" / "fnack.navidrome" / "plugin.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    plugin_dir = str(ROOT / "bundled_plugins" / "fnack.navidrome")
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
    assert "import navidrome" in src
    assert "services.navidrome_service" not in src
    cls = module.NavidromePlugin
    for m in ("trigger_scan", "test_connection", "health", "run_split_repair", "on_load"):
        assert hasattr(cls, m), f"plugin must expose {m}"
    # config is plugin-owned: on_load migrates legacy globals into plugin store
    on_load = inspect.getsource(cls.on_load)
    assert "navidrome_url" in on_load and "context.settings" in on_load

    # the module takes injected config; no core DB reads
    mod_src = (ROOT / "bundled_plugins" / "fnack.navidrome" / "navidrome.py").read_text(encoding="utf-8")
    assert "db.session" not in mod_src, "plugin module must not read core DB for config"
    assert "trigger_navidrome_scan(config" in mod_src


def test_app_and_scripts_resolve_through_plugin() -> None:
    """app.py fix-splits + run_maintenance resolve through the plugin (no
    hidden fallback); fix_navidrome_splits imports the plugin module."""
    app_src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "run_split_repair" in app_src
    assert "services.navidrome_service" not in app_src

    maint = (ROOT / "scripts" / "run_maintenance.py").read_text(encoding="utf-8")
    assert "run_split_repair" in maint
    assert "services.navidrome_service" not in maint

    fix = (ROOT / "scripts" / "fix_navidrome_splits.py").read_text(encoding="utf-8")
    assert "bundled_plugins/fnack.navidrome" in fix
    assert "services.navidrome_service" not in fix


def test_media_service_resolves_plugin_capabilities() -> None:
    """MediaServerService resolves media.scan/health/connection_test — the
    navidrome plugin serves them; the service never names the provider."""
    src = (ROOT / "services" / "media_server_service.py").read_text(encoding="utf-8")
    assert "fnack.navidrome" not in src
    assert "services.navidrome_service" not in src
    for cap in ("media.scan", "media.health", "media.connection_test"):
        assert cap in src


def test_docs_describe_post_extraction_architecture() -> None:
    """Docs attribute Navidrome to the fnack.navidrome plugin + capabilities —
    never to services/navidrome_service.py."""
    for doc in [ROOT / "README.md", ROOT / "DEPLOY.md", ROOT / "docs" / "plugins" / "AUTHORING.md"]:
        text = doc.read_text(encoding="utf-8")
        assert "services/navidrome_service.py" not in text, f"{doc.name} must not name the deleted service"


if __name__ == "__main__":
    test_provider_impl_lives_in_plugin_not_core()
    test_plugin_is_authoritative_and_owns_config()
    test_app_and_scripts_resolve_through_plugin()
    test_media_service_resolves_plugin_capabilities()
    test_docs_describe_post_extraction_architecture()
    print("test_navidrome_extraction: PASSED")
