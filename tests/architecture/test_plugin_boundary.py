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
        "fnack.spotiflac", "fnack.ytdlp", "fnack.spotify", "fnack.deezer-batch",
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


if __name__ == "__main__":
    test_plugins_never_import_core_internals()
    test_transitional_services_imports_are_documented()
    test_plugin_can_import_public_sdk()
    print("test_plugin_boundary: PASSED")
