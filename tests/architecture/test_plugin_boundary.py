"""Architecture test: plugin boundary (Phase 1, MASTER §Architecture tests).

Plugins must use the public SDK / `plugins.base` interfaces — never `models`,
`app`, `plugins.manager`, `plugins.api`, `plugins.registry`, or
`plugins.models` (core internals). The MASTER permits a *temporary*
plugin -> legacy-service adapter during migration (rule 4), so `services.*`
imports inside plugins are flagged as transitional, not forbidden — later
phases delete them as each provider is extracted.

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

# services.* imports are transitional adapters (MASTER rule 4). Track which
# plugins still use them so Phase 2+ extraction is auditable; the test only
# hard-fails on core-internal imports.
SERVICES_IMPORT = re.compile(r"^\s*(?:from|import)\s+services\.", re.MULTILINE)


def _plugin_dirs() -> list[Path]:
    dirs = []
    for root in (ROOT / "bundled_plugins", ROOT / "examples" / "plugins"):
        if root.exists():
            dirs.extend(p for p in root.iterdir() if (p / "plugin.py").exists())
    return dirs


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


def test_transitional_services_imports_are_documented() -> None:
    """Not a hard failure — an inventory so each entry is visible for the
    extraction phases (plugin -> legacy-service adapter, MASTER rule 4)."""
    usage: dict[str, list[str]] = {}
    for pdir in _plugin_dirs():
        for py in pdir.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            if SERVICES_IMPORT.search(text):
                usage.setdefault(pdir.name, []).append(py.name)
    # The expected transitional adapters (all official plugins still wrapping
    # a legacy service). fnack.discord-webhook / ntfy-webhook / subsonic /
    # lidarr / reverse-proxy-auth are already real plugins (no services import).
    print("Transitional plugin->service adapters (to be extracted):")
    for pid, files in sorted(usage.items()):
        print(f"  {pid}: {', '.join(files)}")
    assert set(usage) <= {
        "fnack.ytdlp", "fnack.spotify", "fnack.deezer-batch",
        "fnack.musicbrainz", "fnack.itunes", "fnack.acoustid", "fnack.navidrome",
        "fnack.vpn", "fnack.clean-navidrome-artists", "fnack.fix-navidrome-splits",
        "fnack.normalize-album-tags", "fnack.reverify-library",
    }, "unexpected services.* import in a plugin"


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
    # missing here, this test cannot run (skip, don't fail). The remaining
    # transitional plugins import `services.*` (which need
    # flask/flask_sqlalchemy/yt_dlp).
    try:
        import flask  # noqa: F401
        import flask_sqlalchemy  # noqa: F401
        import yt_dlp  # noqa: F401
        import services.ytdlp_service  # noqa: F401
        import services.spotify_service  # noqa: F401
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
    test_plugins_never_import_core_internals()
    test_transitional_services_imports_are_documented()
    test_plugin_can_import_public_sdk()
    test_official_bundle_capability_registration()
    print("test_plugin_boundary: PASSED")
