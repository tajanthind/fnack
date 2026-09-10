"""Architecture/parity test: documentation gate (Phase 4, PR 6 + final cleanup).

Per the user directive (04-PHASE-4 + "final Phase 4 documentation gate"):
after all six providers are extracted, audit the whole repo for stale
provider-service references and run the documentation/reference grep as a
regression test.

Scope (post-final-cleanup):

- README is the USER-facing document: architecture-light, no plugin
  inventory, no per-plugin config enumeration, long guides moved to
  `docs/guides/`. It must stay valid when plugins are added/removed — so it
  hardcodes no official-plugin list and no plugin IDs.
- `docs/architecture.md` is the DEEP architecture reference: the
  core->service->capability->provider flow, the rules, the official-plugin
  snapshot, and the essential-vs-optional packaging policy.
- CURRENT-STATE docs describe the POST-EXTRACTION architecture: providers are
  official plugins implementing capabilities; core contains no provider
  implementations. They never name the deleted provider services as core
  files.
- Core source must not import the deleted provider services.
- The obsolete core DB model (MusicBrainzCache) is gone — provider cache is
  plugin-owned.

HISTORICAL wayfinder tickets/research (phase-1-design, scale-to-millions,
musicbrainz-integration, the phase-4 extraction plan itself) document what
was planned/decided at the time and legitimately reference the old paths —
they are excluded here (they are records, not current architecture docs).

Run from the repo root:

    .venv/bin/python tests/architecture/test_documentation_gate.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

DELETED_SERVICES = [
    "spotify_service", "deezer_service", "musicbrainz_service",
    "itunes_service", "acoustid_service", "navidrome_service",
]

# Current-state docs that must describe post-extraction architecture.
CURRENT_DOCS = [
    ROOT / "README.md",
    ROOT / "DEPLOY.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "plugins" / "AUTHORING.md",
    ROOT / "wayfinder" / "plugin-architecture-map.md",
]

# Deep architecture reference (developer-facing; README is architecture-light).
ARCHITECTURE_DOC = ROOT / "docs" / "architecture.md"
README = ROOT / "README.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_current_docs_do_not_name_deleted_services() -> None:
    """Current-state docs never reference the deleted provider services as
    core files."""
    for doc in CURRENT_DOCS:
        text = _read(doc)
        for svc in DELETED_SERVICES:
            assert f"services/{svc}.py" not in text, \
                f"{doc.name} must not name services/{svc}.py (post-extraction)"
            # allow "services.deezer_service" only in a "no ... import" context
            if f"services.{svc}" in text:
                lines = [l for l in text.splitlines() if f"services.{svc}" in l]
                for l in lines:
                    assert "no" in l.lower() or "not" in l.lower() or "removed" in l.lower(), \
                        f"{doc.name} describes {svc} as a core service: {l.strip()!r}"


def test_core_source_has_no_deleted_service_imports() -> None:
    """No core source file imports the deleted provider services."""
    for py in [ROOT / "app.py", *(ROOT / "services").glob("*.py"),
               *(ROOT / "plugins").glob("*.py"), *(ROOT / "scripts").glob("*.py")]:
        text = py.read_text(encoding="utf-8")
        for svc in DELETED_SERVICES:
            assert f"services.{svc}" not in text, \
                f"{py.name} still references services.{svc}"


def test_architecture_doc_describes_provider_plugins() -> None:
    """The deep architecture doc describes providers as official plugins
    implementing capabilities."""
    doc = _read(ARCHITECTURE_DOC)
    for plugin in ["fnack.spotiflac", "fnack.ytdlp", "fnack.deezer-batch",
                   "fnack.musicbrainz", "fnack.itunes", "fnack.spotify",
                   "fnack.acoustid", "fnack.navidrome"]:
        assert plugin in doc, f"docs/architecture.md must mention {plugin} as a provider plugin"
    assert "download.track" in doc and "capabilities" in doc
    assert "Core is provider-free" in doc, \
        "docs/architecture.md must state core is provider-free"


def test_architecture_doc_explains_plugin_model() -> None:
    """The deep doc explains the capability/plugin model structurally: the
    core->service->capability->provider flow, per-capability priority,
    disabling/zero-provider semantics, and community replacement."""
    doc = _read(ARCHITECTURE_DOC)
    for needle in [
        "Application service",
        "Capability",
        "Provider registry",
        "Provider plugin",
        "Core is provider-free",
        "Multiple plugins can implement the same capability",
        "Priority is per capability",
        "Disabling a plugin removes its capabilities",
        "Zero providers is a valid state",
        "Community plugins can replace official providers",
        "## Essential vs optional packaging",
    ]:
        assert needle in doc, f"docs/architecture.md must explain: {needle!r}"


def test_readme_is_user_focused_and_architecture_light() -> None:
    """README is user-facing: no architecture section, no plugin inventory,
    no plugin IDs — so it stays valid when plugins are added or removed."""
    readme = _read(README)
    assert "## What fnack is" in readme
    for needle in ["## Quick Start", "## Configuration", "## Plugins",
                   "## Guides", "## License"]:
        assert needle in readme, f"README must have a {needle!r} section"
    assert "## Architecture" not in readme, \
        "README must be architecture-light (deep docs live in docs/architecture.md)"
    # No hardcoded plugin inventory: not a single plugin ID in README.
    assert "fnack." not in readme, \
        "README must not hardcode plugin IDs (inventory lives in fnack-plugins / docs/architecture.md)"
    # The capability explanation stays at a user level, not a spec.
    assert "plugins provide them" in readme


def test_readme_config_is_core_vs_plugin_without_enumeration() -> None:
    """README config: core settings table + plugin-owned settings explained —
    never a per-plugin enumeration of provider settings."""
    readme = _read(README)
    assert "## Configuration" in readme
    config_section = readme.split("## Configuration", 1)[1].split("---", 1)[0]
    assert "core" in config_section.lower() and "plugin" in config_section.lower()
    for key in ["spotiflac_quality", "spotiflac_delay", "ytdlp_format",
                "youtube_cookies_path", "youtube_source", "spotdl_source",
                "navidrome_url", "acoustid_api_key"]:
        assert key not in readme, \
            f"provider setting {key!r} must not appear in README (no per-plugin enumeration)"
    # The migration note explains legacy flat keys move into the plugin.
    assert "migration fallback" in readme


def test_media_scan_vs_server_extension_distinction_in_architecture_doc() -> None:
    """Navidrome (media.scan provider) and server-API plugins
    (server.extension) are different concepts; the deep doc presents them as
    such — and the removed fnack.subsonic plugin is referenced nowhere."""
    doc = _read(ARCHITECTURE_DOC)
    assert "fnack.navidrome" in doc and "media.scan" in doc
    assert "server.extension" in doc
    assert "fnack.subsonic" not in doc, \
        "removed subsonic plugin must not appear in docs/architecture.md"
    media_section = doc.split("### Essential vs optional packaging", 1)[0]
    assert "media-server scan" in media_section and "server API plugins" in media_section


def test_no_provider_implementation_leak() -> None:
    """Docs must not present provider-internal implementation details as core
    functionality (e.g. 'cached Deezer lookups' in a core feature)."""
    for doc in (README, ARCHITECTURE_DOC):
        text = _read(doc)
        for phrase in [
            "cached Deezer lookups",      # Deezer impl detail presented as core
            "Subsonic API Integration",   # stale conflation of Subsonic/Navidrome
        ]:
            assert phrase not in text, f"{doc.name} must not present stale impl detail: {phrase!r}"


def test_guides_moved_out_of_readme() -> None:
    """The long setup guides live in docs/guides/ and README only links to
    them (a regression check that they don't creep back into the README)."""
    readme = _read(README)
    for link in ["docs/guides/youtube-cookies.md", "docs/guides/vpn.md"]:
        assert link in readme, f"README must link to {link}"
    for guide in [ROOT / "docs" / "guides" / "youtube-cookies.md",
                  ROOT / "docs" / "guides" / "vpn.md"]:
        assert guide.exists(), f"guide file missing: {guide}"
    # The old inline guide bodies must be gone from the README.
    for body in ["Get cookies.txt LOCALLY", "Step-by-Step Instructions",
                 "## VPN Setup", "Option A: Upload via the Web UI"]:
        assert body not in readme, f"guide body {body!r} must not be inlined in README"


def test_obsolete_core_db_model_removed() -> None:
    """The MusicBrainz provider cache is plugin-owned; the core DB model is
    gone."""
    models_src = (ROOT / "models.py").read_text(encoding="utf-8")
    assert "MusicBrainzCache" not in models_src
    assert "musicbrainz_cache" not in models_src


def test_essential_packaging_is_documented() -> None:
    """The essential-vs-optional packaging policy is documented in the deep
    architecture doc and named in the README (user-facing, no ID list)."""
    doc = _read(ARCHITECTURE_DOC)
    assert "ESSENTIAL_PLUGINS" in doc and "single source of truth" in doc
    assert "fnack.spotiflac" in doc and "fnack.deezer-batch" in doc
    readme = _read(README)
    assert "default out-of-box experience" in readme
    assert "Marketplace" in readme


def test_wayfinder_map_marks_phase4_complete() -> None:
    """The wayfinder map/tickets mark the extraction work complete and record
    the essential-packaging decision."""
    map_src = _read(ROOT / "wayfinder" / "plugin-architecture-map.md")
    assert "Phase 4" in map_src
    ticket = (ROOT / "wayfinder" / "tickets" / "plugin-phase-4-hardening-deletion.md")
    assert ticket.exists()
    ticket_src = ticket.read_text(encoding="utf-8")
    assert "Extraction 5" in ticket_src  # all six extraction PRs recorded


if __name__ == "__main__":
    test_current_docs_do_not_name_deleted_services()
    test_core_source_has_no_deleted_service_imports()
    test_architecture_doc_describes_provider_plugins()
    test_architecture_doc_explains_plugin_model()
    test_readme_is_user_focused_and_architecture_light()
    test_readme_config_is_core_vs_plugin_without_enumeration()
    test_media_scan_vs_server_extension_distinction_in_architecture_doc()
    test_no_provider_implementation_leak()
    test_guides_moved_out_of_readme()
    test_obsolete_core_db_model_removed()
    test_essential_packaging_is_documented()
    test_wayfinder_map_marks_phase4_complete()
    print("test_documentation_gate: PASSED")
