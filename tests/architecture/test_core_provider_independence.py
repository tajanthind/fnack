"""Architecture test: core provider independence (MASTER §Architecture tests).

The provider extractions are COMPLETE (all six providers moved into their
plugins in Phase 4). This test enforces the final state:

  1. The public SDK (`fnack/plugin_api/`) never imports provider services,
     the DB models, or the Flask app — the SDK is the clean boundary.
  2. Core business logic never reaches into PluginManager's private
     `_plugins` dict — it uses the public API / capability registry.
  3. Core imports NO provider services (the Phase 1/2 transitional allowlist
     shrank entry by entry and is now EMPTY — all six provider services are
     deleted). Adding a NEW provider import fails.
  4. Provider-ID equality branches in core are gone (the transitional
     deezer-batch keying was replaced by the capability boundary). Adding a
     NEW branch fails.
  5. Missing capability is a valid state: CapabilityUnavailable, no fallback.

Run from the repo root:

    .venv/bin/python tests/architecture/test_core_provider_independence.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# 1. SDK purity
# ---------------------------------------------------------------------------

SDK_DIR = ROOT / "fnack" / "plugin_api"


def test_sdk_never_imports_core_internals() -> None:
    forbidden = re.compile(
        r"^\s*(?:from|import)\s+(services\.|models\b|app\b|plugins\.manager|plugins\.api)",
        re.MULTILINE,
    )
    violations = []
    for path in sorted(SDK_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if forbidden.search(line) and not line.strip().startswith("#"):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not violations, (
        "fnack.plugin_api (the public SDK) must not import provider services, "
        f"the DB models, the Flask app, or plugin internals:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 2. No private PluginManager access from core
# ---------------------------------------------------------------------------

PRIVATE_ACCESS_RE = re.compile(r"(?:_pm|plugin_manager|manager)\._plugins\b")


def test_core_uses_public_manager_api() -> None:
    """MASTER/PHASE 1: 'Replace private access such as _pm._plugins[...] with
    public methods.' The manager's own internals are exempt (same package)."""
    scan = [ROOT / "app.py", *(ROOT / "services").glob("*.py"), ROOT / "plugins" / "api.py"]
    violations = []
    for path in scan:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if PRIVATE_ACCESS_RE.search(line) and not line.strip().startswith("#"):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not violations, (
        "Core business logic must use PluginManager's public API "
        f"(get_plugin/get_loaded/get_plugin_capabilities/capability_registry), "
        f"never the private _plugins dict:\n" + "\n".join(violations)
    )


EXECUTOR_BYPASS_RE = re.compile(r"(?:_pm|plugin_manager|_pm2)\.executor\.run\b")


def test_runtime_provider_calls_use_guarded_boundary() -> None:
    """Phase 1.1 review §1: application services must invoke capability
    providers through PluginManager.invoke_provider() — never
    `_pm.executor.run(...)` directly. The executor is the manager's
    invocation MECHANISM; the guarded boundary (timeout + consecutive-
    failure + auto-disable + health) is the API. A direct executor.run in
    app code means a provider can fail without health/auto-disable."""
    scan = [ROOT / "app.py", *(ROOT / "services").glob("*.py")]
    violations = []
    for path in scan:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if EXECUTOR_BYPASS_RE.search(line) and not line.strip().startswith("#"):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not violations, (
        "Application services must invoke providers via "
        "PluginManager.invoke_provider() (the guarded boundary), never "
        f"manager.executor.run() directly:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 3. Provider imports forbidden (transitional allowlist is EMPTY post-Phase-4)
# ---------------------------------------------------------------------------

FORBIDDEN_PROVIDER_MODULES = {
    "services.spotify_service",
    "services.deezer_service",
    "services.musicbrainz_service",
    "services.acoustid_service",
    "services.navidrome_service",
    "services.itunes_service",
}

# Phase 4: all six providers are extracted into their plugins (spotify,
# deezer, musicbrainz, itunes, acoustid, navidrome) and the legacy services
# are deleted. Core imports no provider services; a provider import anywhere
# in core is forbidden.
TRANSITIONAL_PROVIDER_IMPORTS: dict[str, dict[str, str]] = {}

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(services\.\w+)", re.MULTILINE)


def test_core_has_no_provider_imports() -> None:
    scan = [ROOT / "app.py", *(ROOT / "services").glob("*.py")]
    violations = []
    for path in scan:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        allowlist = TRANSITIONAL_PROVIDER_IMPORTS.get(path.name, {})
        for m in IMPORT_RE.finditer(text):
            mod = m.group(1)
            if mod not in FORBIDDEN_PROVIDER_MODULES:
                continue
            if allowlist.get(mod):
                continue  # documented allowlist entry (none remain post-Phase-4)
            violations.append(f"{path.name}: imports {mod}")
    assert not violations, (
        "Provider imports in core are forbidden — all six providers were "
        "extracted into their plugins in Phase 4 and the legacy services are "
        "deleted:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 4. Provider-ID branches frozen
# ---------------------------------------------------------------------------

# Provider-ID branches: equality/identity tests against a provider id, e.g.
# `== "fnack.spotify"`, `!= "fnack.spotify"`, `in ("fnack.a", "fnack.b")`.
# Deliberately narrow: logger names ("fnack.spotify"), DB paths ("fnack.db"),
# and default-manifest sets (allowed by the MASTER) must NOT match.
ID_BRANCH_RE = re.compile(r"""(?:==|!=|is not|is)\s*["']fnack\.[a-z0-9\-]+["']""")

# Transitional: the deezer-batch discography keying (per-provider artist id
# vs name) shipped in PR #6. REMOVED in Phase 3 Step 2 (MetadataService owns
# the artist.discography chain and never branches on provider IDs). No
# entries remain; a new provider-ID branch anywhere in core is forbidden.
TRANSITIONAL_ID_BRANCHES: dict[str, set[str]] = {}


def test_core_has_no_provider_id_branches() -> None:
    scan = [ROOT / "app.py", *(ROOT / "services").glob("*.py")]
    violations = []
    for path in scan:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        allowed = TRANSITIONAL_ID_BRANCHES.get(path.name, set())
        for lineno, line in enumerate(text.splitlines(), 1):
            if not ID_BRANCH_RE.search(line):
                continue
            stripped = line.strip()
            if any(frag in stripped for frag in allowed):
                continue
            violations.append(f"{path.name}:{lineno}: {stripped}")
    assert not violations, (
        "New provider-ID branches in core are forbidden (MASTER §Zero provider "
        "hardwiring). Plugin IDs must not determine provider behavior:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 5. Missing capability is a valid state
# ---------------------------------------------------------------------------

def test_missing_capability_is_a_valid_state() -> None:
    """MASTER rule 3: no provider for a capability -> structured error, no
    hidden fallback."""
    from fnack.plugin_api.capabilities import CapabilityRegistry
    from fnack.plugin_api.errors import CapabilityUnavailable
    from fnack.plugin_api import DOWNLOAD_TRACK, ARTIST_SEARCH

    reg = CapabilityRegistry()
    assert reg.has(DOWNLOAD_TRACK) is False
    assert reg.providers(DOWNLOAD_TRACK) == []
    assert reg.has(ARTIST_SEARCH) is False

    # PluginManager path: no providers -> structured error, never a fallback.
    from plugins.manager import PluginManager
    mgr = PluginManager(plugins_dir="/nonexistent/plugins", core_version="0.0.0")
    try:
        mgr.get_capability_providers(DOWNLOAD_TRACK)
    except CapabilityUnavailable as exc:
        assert exc.capability == DOWNLOAD_TRACK
        assert "No enabled plugin provides capability" in str(exc)
    else:
        raise AssertionError("expected CapabilityUnavailable for unserved capability")


if __name__ == "__main__":
    test_sdk_never_imports_core_internals()
    test_core_uses_public_manager_api()
    test_runtime_provider_calls_use_guarded_boundary()
    test_core_has_no_provider_imports()
    test_core_has_no_provider_id_branches()
    test_missing_capability_is_a_valid_state()
    print("test_core_provider_independence: PASSED")
