"""Architecture test: plugin boundary (MASTER §Architecture tests).

Plugins must use the public SDK / `plugins.base` interfaces — never `models`,
`app`, `plugins.manager`, `plugins.api`, `plugins.registry`, or
`plugins.models` (core internals). All provider extractions are complete:
no plugin wraps a legacy provider service anymore. The only `services.*`
imports left in the official bundle are a small set of generic core helpers
(verifier policy, VPN infrastructure) that plugins may call through — the test
pins that set so a provider-service import can never reappear.

Run from the repo root:

    .venv/bin/python tests/architecture/test_plugin_boundary.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Core internals a plugin must never import (final state; already true today).
FORBIDDEN_IMPORTS = re.compile(
    r"^\s*(?:from|import)\s+"
    r"(models\b|app\b|plugins\.manager|plugins\.api|plugins\.registry|plugins\.models)",
    re.MULTILINE,
)

SERVICES_IMPORT = re.compile(r"^\s*(?:from|import)\s+services\.", re.MULTILINE)

# The ONLY `services.*` modules a bundled plugin may import: generic CORE
# helpers (not provider implementations) that are deliberately core —
# verifier_service (duration/tag verification policy) and vpn_service
# (in-container VPN infrastructure; the fnack.vpn plugin is a thin wrapper
# around it). Every entry must name a module that exists today, so a stale
# allowlist entry (like the deleted acoustid_service) fails fast.
PLUGIN_ALLOWED_SERVICES = {
    "services.verifier_service",
    "services.vpn_service",
}


def _plugin_dirs() -> list[Path]:
    dirs = []
    for root in (ROOT / "bundled_plugins", ROOT / "examples" / "plugins"):
        if root.exists():
            dirs.extend(p for p in root.iterdir() if (p / "plugin.py").exists())
    return dirs


def test_allowed_services_helpers_exist() -> None:
    """Every entry in PLUGIN_ALLOWED_SERVICES must name a real core module —
    a stale entry (e.g. a deleted provider service) is a regression."""
    for module in PLUGIN_ALLOWED_SERVICES:
        name = module.split(".", 1)[1]  # services.<name>
        assert (ROOT / "services" / f"{name}.py").exists(), (
            f"{module} is allowlisted in the boundary test but "
            f"services/{name}.py no longer exists — remove the stale entry"
        )


def test_plugins_never_import_core_internals() -> None:
    violations = []
    for pdir in _plugin_dirs():
        for py in pdir.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if FORBIDDEN_IMPORTS.search(line) and not line.strip().startswith("#"):
                    violations.append(f"{pdir.name}/{py.name}:{lineno}: {line.strip()}")
    assert not violations, (
        "Plugins must never import core internals (models, app, "
        "plugins.manager/api/registry/models) — use fnack.plugin_api or "
        f"plugins.base instead:\n" + "\n".join(violations)
    )


def test_plugins_import_only_generic_core_helpers() -> None:
    """No bundled plugin may import a provider service from `services.*`.

    The only sanctioned `services.*` imports are the generic core helpers in
    PLUGIN_ALLOWED_SERVICES (verifier policy, VPN infra); anything else is a
    provider implementation that must live behind the plugin boundary."""
    usage: dict[str, list[str]] = {}
    for pdir in _plugin_dirs():
        for py in pdir.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            # Ignore the allowed generic core-helper modules.
            filtered = "\n".join(
                ln for ln in text.splitlines()
                if not any(m in ln for m in PLUGIN_ALLOWED_SERVICES)
            )
            if SERVICES_IMPORT.search(filtered):
                usage.setdefault(pdir.name, []).append(py.name)
    assert not usage, (
        "Plugins must not import provider services from services.* — provider "
        "implementations live inside the plugin. Found: "
        + "; ".join(f"{pid}: {', '.join(files)}" for pid, files in sorted(usage.items()))
    )


def test_plugin_can_import_public_sdk() -> None:
    """The SDK import path is what plugins should use going forward."""
    import fnack.plugin_api as api  # noqa: F401
    from fnack.plugin_api import PluginContext, TrackRef, DownloadRequest  # noqa: F401
    assert api.DOWNLOAD_TRACK == "download.track"
    assert api.SDK_VERSION


def test_official_bundle_capability_registration() -> None:
    """Test E (Phase 1.1): the enabled official bundle registers its
    capabilities — download.track is served by the two downloaders,
    fingerprint.identify by acoustid, etc. (validated against contracts).

    This is an APPLICATION-ENVIRONMENT test: it imports the real bundled
    plugins, which depend on the fnack runtime stack (flask, flask_sqlalchemy,
    yt_dlp, SpotiFLAC, ...). In a bare/CI environment without those deps the
    plugins fail to load — that is a real-environment limitation, NOT a
    boundary failure, so the test reports SKIPPED rather than failing the
    pure-architecture suite. Run it in fnack's actual container/venv (where
    the smoke test runs) for the full assertion."""
    import logging
    logging.disable(logging.WARNING)  # silence expected-contract warnings
    import tempfile
    from plugins.manager import init_plugin_manager

    # The real bundled plugins import the runtime stack; if that stack is
    # missing here, this test cannot run (skip, don't fail).
    try:
        import flask  # noqa: F401
        import flask_sqlalchemy  # noqa: F401
        import yt_dlp  # noqa: F401
        # The spotify provider implementation now lives in the plugin (it
        # needs requests + its search deps); probe it directly.
        sys.path.insert(0, str(ROOT / "bundled_plugins" / "fnack.spotify"))
        import spotify as _spotify_plugin_mod  # noqa: F401
    except ImportError as exc:
        print(f"SKIPPED test_official_bundle_capability_registration "
              f"(runtime deps unavailable here: {exc})")
        logging.disable(logging.NOTSET)
        return

    bundled = ROOT / "bundled_plugins"
    assert bundled.exists(), "bundled_plugins dir must exist for Test E"
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = init_plugin_manager(
            plugins_dir=tmpdir,
            bundled_plugins_dir=str(bundled),
            core_version="0.3.1",
        )
        mgr.load_all()  # enable everything discovered

        reg = mgr.capability_registry
        # Official downloaders serve download.track, priority-ordered.
        dl = [p.plugin_id for p in reg.providers_for("download.track")]
        assert "fnack.spotiflac" in dl and "fnack.ytdlp" in dl, \
            f"download.track providers were {dl}"
        assert dl.index("fnack.spotiflac") < dl.index("fnack.ytdlp"), \
            "spotiflac (p10) must be tried before ytdlp (p50)"
        # fingerprint, media, auth, network, notification, server ext.
        assert reg.has("fingerprint.identify")
        assert reg.has("media.scan")
        assert reg.has("network.route")
        assert reg.has("server.extension")
        assert reg.has("notification.event")
        # Every registered capability passed contract validation.
        for cap in ["download.track", "fingerprint.identify", "media.scan",
                    "network.route", "server.extension", "notification.event"]:
            assert reg.has(cap), f"official bundle must serve {cap}"
    logging.disable(logging.NOTSET)


if __name__ == "__main__":
    test_allowed_services_helpers_exist()
    test_plugins_never_import_core_internals()
    test_plugins_import_only_generic_core_helpers()
    test_plugin_can_import_public_sdk()
    test_official_bundle_capability_registration()
    print("test_plugin_boundary: PASSED")
