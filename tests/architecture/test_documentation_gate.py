"""Architecture/parity test: documentation gate (Phase 4, PR 6).

Per the user directive (04-PHASE-4 + "final Phase 4 documentation gate"):
after all six providers are extracted, audit the whole repo for stale
provider-service references and run the documentation/reference grep as a
regression test.

Scope:
- CURRENT-STATE docs (README, DEPLOY, docs/plugins/AUTHORING.md,
  wayfinder/plugin-architecture-map.md) must describe the POST-EXTRACTION
  architecture: providers are official plugins implementing capabilities;
  core contains no provider implementations. They must NOT name the deleted
  services/spotify_service.py etc. or describe a provider as a core service.
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
    ROOT / "docs" / "plugins" / "AUTHORING.md",
    ROOT / "wayfinder" / "plugin-architecture-map.md",
]


def test_current_docs_do_not_name_deleted_services() -> None:
    """Current-state docs never reference the deleted provider services as
    core files."""
    for doc in CURRENT_DOCS:
        text = doc.read_text(encoding="utf-8")
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


def test_providers_are_plugins_with_capabilities() -> None:
    """Current docs describe providers as official plugins implementing
    capabilities."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for plugin in ["fnack.spotiflac", "fnack.ytdlp", "fnack.deezer-batch",
                   "fnack.musicbrainz", "fnack.itunes", "fnack.spotify",
                   "fnack.acoustid", "fnack.navidrome"]:
        assert plugin in readme, f"README must mention {plugin} as a provider plugin"
    assert "download.track" in readme or "capabilities" in readme
    assert "provider-free" in readme, "README must state core is provider-free"


def test_readme_explains_plugin_model() -> None:
    """The README must explain the capability/plugin model structurally (not
    just not-name deleted services): the core→service→capability→provider
    flow, per-capability priority, disabling/zero-provider semantics, and
    community replacement."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    # The architecture section must show the resolution flow and the key rules.
    for needle in [
        "## Architecture",
        "Core is provider-free",
        "Provider registry",
        "Multiple plugins can implement the same capability",
        "Priority is per capability",
        "Disabling a plugin removes its capabilities",
        "Zero providers is a valid state",
        "Community plugins can replace official providers",
        "## Plugins",
    ]:
        assert needle in readme, f"README must explain: {needle!r}"
    # The provider flow diagram: core → service → capability → provider.
    assert "Application service" in readme and "Capability" in readme


def test_readme_config_split_does_not_leak_provider_settings_as_core() -> None:
    """Provider settings (spotiflac_quality/ytdlp_format/...) must be presented
    as PLUGIN configuration, not as flat core settings."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Configuration" in readme
    assert "### Core configuration" in readme
    assert "### Plugin configuration" in readme
    # Provider-owned keys must appear only under the plugin table / migration
    # note, never as core settings.
    core_section = readme.split("### Core configuration", 1)[1].split("### Plugin configuration", 1)[0]
    for key in ["spotiflac_quality", "spotiflac_delay", "ytdlp_format",
                "youtube_cookies_path", "youtube_source", "spotdl_source"]:
        assert key not in core_section, \
            f"provider setting {key!r} must not appear in the core config table"
    # The migration note explains the legacy keys move into the plugin.
    assert "migration fallback" in readme


def test_readme_distinguishes_media_scan_from_subsonic() -> None:
    """Navidrome (media.scan provider) and Subsonic (server.extension plugin)
    are different concepts and must be presented as such."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "fnack.navidrome" in readme and "media.scan" in readme
    assert "fnack.subsonic" in readme and "server.extension" in readme
    # The media-server section must separate the two (a single line that
    # collapses them — e.g. "Subsonic API Integration ... Navidrome" — is
    # stale).
    media_section = readme.split("### Media-server integration", 1)[1].split("---", 1)[0]
    assert "Media-server scan" in media_section and "Subsonic/OpenSubsonic server" in media_section


def test_readme_no_provider_implementation_leak() -> None:
    """README must not present provider-internal implementation details as
    core functionality (e.g. 'cached Deezer lookups' in a core feature)."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for phrase in [
        "cached Deezer lookups",      # Deezer impl detail presented as core
        "Subsonic API Integration",   # stale conflation of Subsonic/Navidrome
    ]:
        assert phrase not in readme, f"README must not present stale impl detail: {phrase!r}"


def test_obsolete_core_db_model_removed() -> None:
    """The MusicBrainz provider cache is plugin-owned; the core DB model is
    gone."""
    models_src = (ROOT / "models.py").read_text(encoding="utf-8")
    assert "MusicBrainzCache" not in models_src
    assert "musicbrainz_cache" not in models_src


def test_wayfinder_map_marks_phase4_complete() -> None:
    """The wayfinder map/tickets mark the extraction work complete."""
    map_src = (ROOT / "wayfinder" / "plugin-architecture-map.md").read_text(encoding="utf-8")
    assert "Phase 4" in map_src
    ticket = (ROOT / "wayfinder" / "tickets" / "plugin-phase-4-hardening-deletion.md")
    assert ticket.exists()
    ticket_src = ticket.read_text(encoding="utf-8")
    assert "Extraction 5" in ticket_src  # all six extraction PRs recorded


if __name__ == "__main__":
    test_current_docs_do_not_name_deleted_services()
    test_core_source_has_no_deleted_service_imports()
    test_providers_are_plugins_with_capabilities()
    test_readme_explains_plugin_model()
    test_readme_config_split_does_not_leak_provider_settings_as_core()
    test_readme_distinguishes_media_scan_from_subsonic()
    test_readme_no_provider_implementation_leak()
    test_obsolete_core_db_model_removed()
    test_wayfinder_map_marks_phase4_complete()
    print("test_documentation_gate: PASSED")
