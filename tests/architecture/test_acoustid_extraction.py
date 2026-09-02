"""Architecture/parity test: AcoustID provider extraction (Phase 4, PR 4).

Verifies the Phase 4 extraction contract for fnack.acoustid:

1. The provider implementation lives in the plugin (`acoustid.py`), NOT in
   `services/` — `services/acoustid_service.py` is deleted and no core file
   imports it.
2. The plugin is AUTHORITATIVE: it owns the implementation AND the api_key
   (plugin setting, injected via set_api_key()); serves fingerprint.identify;
   exposes identify_candidates / verify_download / last_lookup_flags helpers
   the core manual-identify route and context facade use through the plugin
   boundary.
3. app.py's manual-identify route resolves the plugin via the
   fingerprint.identify capability — no core acoustid import.
4. plugins/context.py verify_download_acoustid resolves through the plugin;
   fnack.ytdlp's standalone fallback no longer references acoustid_service.
5. The plugin imports no core models (plugin-owned key + cache state).
6. Docs describe the post-extraction architecture.

Run from the repo root:

    .venv/bin/python tests/architecture/test_acoustid_extraction.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_provider_impl_lives_in_plugin_not_core() -> None:
    """The AcoustID implementation moved into the plugin; the core service is
    gone; no core file imports it."""
    plugin_module = ROOT / "bundled_plugins" / "fnack.acoustid" / "acoustid.py"
    assert plugin_module.exists(), "plugin-owned acoustid.py must exist"
    assert not (ROOT / "services" / "acoustid_service.py").exists(), \
        "services/acoustid_service.py must be deleted"

    for py in [ROOT / "app.py", ROOT / "plugins" / "context.py",
               ROOT / "bundled_plugins" / "fnack.ytdlp" / "ytdlp.py",
               ROOT / "services" / "verification_service.py"]:
        text = py.read_text(encoding="utf-8")
        assert "services.acoustid_service" not in text, \
            f"{py.name} still imports the deleted service"


def test_plugin_is_authoritative_and_owns_key() -> None:
    """The plugin imports its own acoustid.py, injects its api_key, and
    exposes the helper methods; it imports no core models."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fnack_plugin_fnack_acoustid",
        ROOT / "bundled_plugins" / "fnack.acoustid" / "plugin.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    plugin_dir = str(ROOT / "bundled_plugins" / "fnack.acoustid")
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
    assert "import acoustid" in src
    assert "services.acoustid_service" not in src
    cls = module.AcoustIDFingerprinter
    for m in ("is_enabled", "identify", "identify_candidates",
              "verify_download", "last_lookup_flags", "on_load",
              "on_settings_changed"):
        assert hasattr(cls, m), f"plugin must expose {m}"
    # key injection: on_load + on_settings_changed call set_api_key
    on_load = inspect.getsource(cls.on_load)
    assert "set_api_key" in on_load, "plugin must inject its api_key"

    ac_src = (ROOT / "bundled_plugins" / "fnack.acoustid" / "acoustid.py").read_text(encoding="utf-8")
    assert "from models import" not in ac_src, "plugin module must not import core models"
    assert "db.session" not in ac_src, "plugin module must not touch core DB for its key"
    assert "set_api_key" in ac_src


def test_app_manual_identify_resolves_plugin_via_capability() -> None:
    """app.py's manual-identify route resolves the provider through the
    fingerprint.identify capability (no core acoustid import)."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "FINGERPRINT_IDENTIFY" in src
    assert "identify_candidates" in src
    assert "services.acoustid_service" not in src


def test_context_facade_resolves_through_plugin() -> None:
    """plugins/context.py verify_download_acoustid resolves through the
    fingerprint.identify capability."""
    src = (ROOT / "plugins" / "context.py").read_text(encoding="utf-8")
    assert "FINGERPRINT_IDENTIFY" in src
    assert "services.acoustid_service" not in src


def test_ytdlp_standalone_fallback_no_acoustid_ref() -> None:
    """fnack.ytdlp's standalone fallback no longer references
    services.acoustid_service (the runtime injects facade callables)."""
    src = (ROOT / "bundled_plugins" / "fnack.ytdlp" / "ytdlp.py").read_text(encoding="utf-8")
    assert "services.acoustid_service" not in src


def test_verification_service_imports_no_acoustid() -> None:
    """VerificationService (provider-neutral) does not import acoustid — it
    consumes normalized evidence (comments may name the plugin)."""
    src = (ROOT / "services" / "verification_service.py").read_text(encoding="utf-8")
    assert "services.acoustid_service" not in src
    # Only application services + generic core helpers (never provider impls)
    assert "services.fingerprint_service" in src
    assert "services.verifier_service" in src


def test_docs_describe_post_extraction_architecture() -> None:
    """Docs attribute AcoustID to the fnack.acoustid plugin + capabilities —
    never to services/acoustid_service.py."""
    for doc in [ROOT / "README.md", ROOT / "docs" / "plugins" / "AUTHORING.md"]:
        text = doc.read_text(encoding="utf-8")
        assert "services/acoustid_service.py" not in text, f"{doc.name} must not name the deleted service"


if __name__ == "__main__":
    test_provider_impl_lives_in_plugin_not_core()
    test_plugin_is_authoritative_and_owns_key()
    test_app_manual_identify_resolves_plugin_via_capability()
    test_context_facade_resolves_through_plugin()
    test_ytdlp_standalone_fallback_no_acoustid_ref()
    test_verification_service_imports_no_acoustid()
    test_docs_describe_post_extraction_architecture()
    print("test_acoustid_extraction: PASSED")
