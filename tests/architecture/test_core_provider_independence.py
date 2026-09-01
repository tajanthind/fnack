"""Architecture test: core provider independence (Phase 1, MASTER §Architecture tests).

Phase 1 establishes the boundary; it does NOT move provider implementations
yet (that is Phases 2-10, per the handoff PR plan). So this test enforces
what Phase 1 can honestly guarantee:

  1. The public SDK (`fnack/plugin_api/`) never imports provider services,
     the DB models, or the Flask app — the SDK is the clean boundary.
  2. Core business logic no longer reaches into PluginManager's private
     `_plugins` dict — it uses the public API / capability registry.
  3. Provider-implementation imports in core are frozen to a documented
     TRANSITIONAL allowlist (plugin -> legacy-service adapters, which the
     MASTER permits "only during migration"). Adding a NEW import fails.
  4. Provider-ID equality branches in core are frozen to the documented
     transitional deezer-batch keying (to be replaced when the metadata
     capability boundary lands). Adding a NEW branch fails.
  5. Missing capability is a valid state: CapabilityUnavailable, no fallback.

IMPORTANT — what this test is and is not (Phase 1.1 review):

- It is a REGRESSION FENCE, not a final architectural guarantee. Passing it
  does NOT mean core is provider-independent today: the TRANSITIONAL
  allowlist below still permits core -> provider-service imports (queue,
  app, import_service) and one provider-ID branch. Those exist because
  Phase 1/1.1 deliberately did not move provider implementations.
- Phase 2's job is to SHRINK this allowlist entry by entry as each provider
  is extracted (each entry names its removal phase). The fence's real value
  is that any NEW import/branch added without extending the allowlist fails
  CI immediately.
- The final state (Definition of Done in the MASTER) is: allowlist empty,
  core provider imports = 0, provider-ID branches = 0.

As later phases remove each transitional entry, this allowlist shrinks.

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
# 3. Provider imports frozen to the transitional allowlist
# ---------------------------------------------------------------------------

FORBIDDEN_PROVIDER_MODULES = {
    "services.spotify_service",
    "services.deezer_service",
    "services.musicbrainz_service",
    "services.acoustid_service",
    "services.navidrome_service",
    "services.itunes_service",
}

# Transitional: plugin -> legacy-service adapters (MASTER rule 4) and the
# confirmed interactive-search split (HARNESS §2 keeps /api/search-artist
# core, calling the bundled Deezer provider directly). Each entry maps
# file -> {module: reason}. Later phases delete these entries.
TRANSITIONAL_PROVIDER_IMPORTS: dict[str, dict[str, str]] = {
    "app.py": {
        "services.deezer_service": "onboarding artist-info lookup (get_artist_info — no capability yet; Phase 6 removes)",
        "services.navidrome_service": "scan/test direct fallback when no scan_trigger plugin (Phase 10 removes)",
        "services.musicbrainz_service": "discography enrichment helper called from sync (Phase 7 removes)",
        "services.acoustid_service": "identify/last-lookup helpers on verify route (Phase 9 removes)",
    },
    "import_service.py": {
        "services.musicbrainz_service": "batch enrichment via metadata chain (Phase 7 removes)",
    },
    "queue_service.py": {
        "services.navidrome_service": "scan fallback (Phase 10 removes)",
        "services.acoustid_service": "fingerprint fallback (Phase 9 removes)",
    },
    "deezer_service.py": {
        "services.itunes_service": "regional album fallback inside provider (Phase 7 removes)",
    },
    "tag_normalization_service.py": {
        "services.navidrome_service": "split-repair scan helper (Phase 10 removes)",
    },
}

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(services\.\w+)", re.MULTILINE)


def test_provider_imports_frozen_to_transitional_allowlist() -> None:
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
                continue  # documented transitional adapter
            violations.append(f"{path.name}: imports {mod} (not in transitional allowlist)")
    assert not violations, (
        "New provider imports in core are forbidden. If you are genuinely "
        "migrating, extend the TRANSITIONAL_PROVIDER_IMPORTS allowlist with a "
        "reason and a phase that removes it:\n" + "\n".join(violations)
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


def test_provider_id_branches_frozen_to_transitional_allowlist() -> None:
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
    test_provider_imports_frozen_to_transitional_allowlist()
    test_provider_id_branches_frozen_to_transitional_allowlist()
    test_missing_capability_is_a_valid_state()
    print("test_core_provider_independence: PASSED")
