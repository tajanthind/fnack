"""Architecture test: multi-capability plugins (Phase 1, MASTER rule 5 +
PHASE 1 §Manifest capability declaration; Phase 1.1 §2 validation).

A single plugin can declare many capabilities in its manifest; the
PluginManager registers the VALID ones with the CapabilityRegistry on
load/enable, and they disappear on disable/unload (MASTER rule 2).

Phase 1.1 adds capability-contract validation: a declared capability the
plugin does not actually implement is SKIPPED (not the whole plugin) with a
clear warning — the valid capabilities from the same plugin still load.

Run from the repo root:

    .venv/bin/python tests/architecture/test_multi_capability_plugins.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from fnack.plugin_api.capabilities import CapabilityRegistry
from fnack.plugin_api import (
    MEDIA_CONNECTION_TEST,
    MEDIA_SCAN,
    SERVER_EXTENSION,
    TRACK_RESOLVE,
)


def _make_plugin_dir(tmp: Path, plugin_id: str, capabilities: list[str], plugin_cls: str,
                    ptype: str = "server_extension") -> Path:
    pdir = tmp / plugin_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(
        f'{{"id":"{plugin_id}","name":"{plugin_id}","version":"1.0.0",'
        f'"type":["{ptype}"],"api_version":"^1.0","min_core_version":"0.2.0",'
        f'"entry_point":"plugin:{plugin_cls}","author":"test",'
        f'"description":"multi-capability fixture","permissions":[],'
        f'"capabilities":{capabilities},'
        f'"settings_schema":[],"ui":{{"slots":[]}},"dependencies":{{}},"trust_level":"community"}}'
    )
    return pdir


def test_manifest_declares_multiple_capabilities() -> None:
    """End-to-end: manifest -> load_plugin -> registry has all VALID
    capabilities, from ONE plugin instance (Test B: multiple capabilities
    remain one plugin)."""
    import tempfile
    from plugins.manager import init_plugin_manager

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # A multi-capability scan_trigger plugin: media.scan + media.connection_test.
        media_like = _make_plugin_dir(
            tmp, "fnack.media-like",
            '["media.scan", "media.connection_test"]',
            "MultiCapPlugin", ptype="scan_trigger",
        )
        (media_like / "plugin.py").write_text(
            "from plugins.base import ScanTriggerPlugin\n"
            "class MultiCapPlugin(ScanTriggerPlugin):\n"
            "    def trigger_scan(self): return True, 'ok'\n"
            "    def test_connection(self): return True, 'ok'\n"
        )
        # A single-capability plugin.
        resolver_like = _make_plugin_dir(
            tmp, "fnack.resolver-like", '["track.resolve"]', "ResolverPlugin",
        )
        (resolver_like / "plugin.py").write_text(
            "from plugins.base import MetadataProviderPlugin\n"
            "class ResolverPlugin(MetadataProviderPlugin):\n"
            "    priority = 30\n"
            "    def search_artist(self, name): return []\n"
            "    def get_artist_discography(self, provider_artist_id): return {'artist_name': '', 'albums': []}\n"
            "    def resolve_track_url(self, song_name, artist_name, **kw): return 'https://open.spotify.com/track/x'\n"
        )

        mgr = init_plugin_manager(
            plugins_dir=str(tmp),
            bundled_plugins_dir=None,
            core_version="0.3.1",
        )
        mgr.load_all()  # enables everything discovered

        # Declared capabilities are what the manager registers.
        assert set(mgr.get_plugin_capabilities("fnack.media-like")) == {
            MEDIA_SCAN, MEDIA_CONNECTION_TEST,
        }
        assert mgr.get_plugin_capabilities("fnack.resolver-like") == [TRACK_RESOLVE]

        reg: CapabilityRegistry = mgr.capability_registry
        assert reg.has(MEDIA_SCAN)
        assert reg.has(MEDIA_CONNECTION_TEST)
        assert reg.has(TRACK_RESOLVE)
        # Each capability resolves to the declaring plugin.
        assert [h.plugin_id for h in reg.providers(MEDIA_SCAN)] == ["fnack.media-like"]
        assert [h.plugin_id for h in reg.providers(TRACK_RESOLVE)] == ["fnack.resolver-like"]

        # Test B: one plugin instance, many capabilities — exactly ONE
        # registry handle and ONE loaded instance.
        assert len(reg.providers(MEDIA_SCAN)) == 1
        assert reg.capabilities_for("fnack.media-like") == sorted({
            MEDIA_SCAN, MEDIA_CONNECTION_TEST,
        })
        loaded = mgr.get_loaded("fnack.media-like")
        assert loaded is not None
        # The same instance serves both capabilities.
        scan_handle = reg.providers(MEDIA_SCAN)[0]
        conn_handle = reg.providers(MEDIA_CONNECTION_TEST)[0]
        assert scan_handle.provider is loaded.instance
        assert conn_handle.provider is loaded.instance


def test_type_derivation_when_manifest_omits_capabilities() -> None:
    """Manifests without `capabilities` derive them from `type` (downloader ->
    download.track, etc.) so third-party plugins still register sensibly."""
    import tempfile
    from plugins.base import DownloaderPlugin, DownloadResult, TrackRef
    from plugins.manager import init_plugin_manager

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        pdir = tmp / "community.downloader"
        pdir.mkdir(parents=True)
        (pdir / "plugin.json").write_text(
            '{"id":"community.downloader","name":"Community DL","version":"1.0.0",'
            '"type":["downloader"],"api_version":"^1.0","min_core_version":"0.2.0",'
            '"entry_point":"plugin:CommunityDL","author":"community","description":"x",'
            '"permissions":[],"settings_schema":[],"ui":{"slots":[]},'
            '"dependencies":{},"trust_level":"community"}'
        )
        (pdir / "plugin.py").write_text(
            "from plugins.base import DownloaderPlugin, DownloadResult, TrackRef\n"
            "class CommunityDL(DownloaderPlugin):\n"
            "    priority = 5\n"
            "    def can_handle(self, track: TrackRef): return True\n"
            "    def download(self, track, dest_dir, options): return DownloadResult(success=True)\n"
        )
        mgr = init_plugin_manager(plugins_dir=str(tmp), bundled_plugins_dir=None, core_version="0.3.1")
        mgr.load_all()
        assert mgr.get_plugin_capabilities("community.downloader") == ["download.track"]
        assert mgr.capability_registry.has("download.track")


def test_invalid_capability_declaration_is_skipped() -> None:
    """Test C (Phase 1.1 §2): a manifest declaring a capability the plugin does
    not implement gets that capability SKIPPED — the valid ones from the same
    plugin still load, and no cryptic AttributeError is raised later."""
    import tempfile
    from plugins.manager import init_plugin_manager

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # Declares media.scan (implemented), media.health (NOT implemented —
        # no health() method), server.extension (NOT implemented — not a
        # ServerExtensionPlugin).
        pdir = _make_plugin_dir(
            tmp, "fnack.partial",
            '["media.scan", "media.health", "server.extension"]',
            "PartialPlugin", ptype="scan_trigger",
        )
        (pdir / "plugin.py").write_text(
            "from plugins.base import ScanTriggerPlugin\n"
            "class PartialPlugin(ScanTriggerPlugin):\n"
            "    def trigger_scan(self): return True, 'ok'\n"
            "    def test_connection(self): return True, 'ok'\n"
        )
        mgr = init_plugin_manager(plugins_dir=str(tmp), bundled_plugins_dir=None, core_version="0.3.1")
        mgr.load_all()

        # Valid capability registered; invalid ones NOT.
        assert mgr.capability_registry.has(MEDIA_SCAN) is True
        assert mgr.capability_registry.has("media.health") is False
        assert mgr.capability_registry.has(SERVER_EXTENSION) is False
        # get_plugin_capabilities reflects the VALIDATED set.
        assert mgr.get_plugin_capabilities("fnack.partial") == [MEDIA_SCAN]
        # The plugin itself still loaded and enabled (not all-or-nothing).
        loaded = mgr.get_loaded("fnack.partial")
        assert loaded is not None and loaded.enabled


def test_disable_removes_all_capabilities() -> None:
    """Test F (MASTER rule 2): disabling a plugin removes ALL capabilities it
    provides — no hidden fallback."""
    import tempfile
    from plugins.manager import init_plugin_manager

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        pdir = _make_plugin_dir(
            tmp, "fnack.disable-me",
            '["media.scan", "media.connection_test"]',
            "DisableMe", ptype="scan_trigger",
        )
        (pdir / "plugin.py").write_text(
            "from plugins.base import ScanTriggerPlugin\n"
            "class DisableMe(ScanTriggerPlugin):\n"
            "    def trigger_scan(self): return True, 'ok'\n"
            "    def test_connection(self): return True, 'ok'\n"
        )
        mgr = init_plugin_manager(plugins_dir=str(tmp), bundled_plugins_dir=None, core_version="0.3.1")
        mgr.load_all()
        assert mgr.capability_registry.has(MEDIA_SCAN)
        assert mgr.capability_registry.has(MEDIA_CONNECTION_TEST)

        mgr.disable_plugin("fnack.disable-me")
        assert mgr.capability_registry.has(MEDIA_SCAN) is False
        assert mgr.capability_registry.has(MEDIA_CONNECTION_TEST) is False
        assert mgr.capability_registry.providers(MEDIA_SCAN) == []
        assert mgr.capability_registry.providers(MEDIA_CONNECTION_TEST) == []

        # Re-enable brings them all back.
        mgr.enable_plugin("fnack.disable-me")
        assert mgr.capability_registry.has(MEDIA_SCAN) is True
        assert mgr.capability_registry.has(MEDIA_CONNECTION_TEST) is True


def test_priority_override_flows_into_registry() -> None:
    """The user-facing plugin-level priority override must reorder capability
    providers; per-capability priority (Phase 1.1) overrides it."""
    import tempfile
    from plugins.manager import init_plugin_manager

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for pid, prio in (("fnack.first", 10), ("fnack.second", 50)):
            pdir = _make_plugin_dir(tmp, pid, '["download.track"]', "DL")
            (pdir / "plugin.py").write_text(
                "from plugins.base import DownloaderPlugin, DownloadResult, TrackRef\n"
                f"class DL(DownloaderPlugin):\n"
                f"    priority = {prio}\n"
                "    def can_handle(self, track: TrackRef): return True\n"
                "    def download(self, track, dest_dir, options): return DownloadResult(success=True)\n"
            )
        mgr = init_plugin_manager(plugins_dir=str(tmp), bundled_plugins_dir=None, core_version="0.3.1")
        mgr.load_all()
        assert [h.plugin_id for h in mgr.capability_registry.providers("download.track")] == [
            "fnack.first", "fnack.second",
        ]
        # Plugin-level override: make fnack.second the first provider.
        loaded = mgr.get_loaded("fnack.second")
        loaded.priority_override = 1
        mgr.refresh_capability_registration("fnack.second")
        assert [h.plugin_id for h in mgr.capability_registry.providers("download.track")] == [
            "fnack.second", "fnack.first",
        ]
        # Capability-specific override (Phase 1.1): demote fnack.first to 100
        # — the plugin-level defaults still apply to unset capabilities, but
        # this capability reorders.
        mgr.set_capability_priority("fnack.first", "download.track", 100)
        assert [h.plugin_id for h in mgr.capability_registry.providers("download.track")] == [
            "fnack.second", "fnack.first",
        ]
        # Setting the capability priority BELOW the other provider reorders.
        mgr.set_capability_priority("fnack.first", "download.track", 1)
        assert [h.plugin_id for h in mgr.capability_registry.providers("download.track")] == [
            "fnack.first", "fnack.second",
        ]
        # Clearing restores the plugin-level default: fnack.first -> 10,
        # fnack.second still has plugin-level override 1 -> second first.
        mgr.set_capability_priority("fnack.first", "download.track", None)
        assert [h.plugin_id for h in mgr.capability_registry.providers("download.track")] == [
            "fnack.second", "fnack.first",
        ]
        # Reject unknown capability and priority < 1.
        try:
            mgr.set_capability_priority("fnack.first", "not.a.capability", 5)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for unknown capability")
        try:
            mgr.set_capability_priority("fnack.first", "download.track", 0)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for priority < 1")


if __name__ == "__main__":
    test_manifest_declares_multiple_capabilities()
    test_type_derivation_when_manifest_omits_capabilities()
    test_invalid_capability_declaration_is_skipped()
    test_disable_removes_all_capabilities()
    test_priority_override_flows_into_registry()
    print("test_multi_capability_plugins: PASSED")
