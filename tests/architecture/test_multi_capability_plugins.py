"""Architecture test: multi-capability plugins (Phase 1, MASTER rule 5 +
PHASE 1 §Manifest capability declaration).

A single plugin can declare many capabilities in its manifest; the
PluginManager registers them all with the CapabilityRegistry on load/enable,
and they disappear on disable/unload (MASTER rule 2).

Run from the repo root:

    .venv/bin/python tests/architecture/test_multi_capability_plugins.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from fnack.plugin_api.capabilities import CapabilityRegistry
from fnack.plugin_api import (
    ALBUM_METADATA,
    ARTIST_DISCOGRAPHY,
    ARTIST_SEARCH,
    MEDIA_CONNECTION_TEST,
    MEDIA_HEALTH,
    MEDIA_SCAN,
    SERVER_EXTENSION,
    TRACK_METADATA,
    TRACK_RESOLVE,
)


def _make_plugin_dir(tmp: Path, plugin_id: str, capabilities: list[str], plugin_cls: str) -> Path:
    pdir = tmp / plugin_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(
        f'{{"id":"{plugin_id}","name":"{plugin_id}","version":"1.0.0",'
        f'"type":["server_extension"],"api_version":"^1.0","min_core_version":"0.2.0",'
        f'"entry_point":"plugin:{plugin_cls}","author":"test",'
        f'"description":"multi-capability fixture","permissions":[],'
        f'"capabilities":{capabilities},'
        f'"settings_schema":[],"ui":{{"slots":[]}},"dependencies":{{}},"trust_level":"community"}}'
    )
    return pdir


def test_manifest_declares_multiple_capabilities() -> None:
    """End-to-end: manifest -> load_plugin -> registry has all capabilities."""
    import tempfile
    from plugins.base import ServerExtensionPlugin
    from plugins.manager import init_plugin_manager

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # A multi-capability plugin: 4 metadata caps + server extension.
        navidrome_like = _make_plugin_dir(
            tmp, "fnack.navidrome-like",
            '["media.scan", "media.health", "media.connection_test", "server.extension"]',
            "MultiCapPlugin",
        )
        (navidrome_like / "plugin.py").write_text(
            "from plugins.base import ServerExtensionPlugin\n"
            "class MultiCapPlugin(ServerExtensionPlugin):\n"
            "    def register_routes(self, blueprint): pass\n"
        )
        # A single-capability plugin.
        resolver_like = _make_plugin_dir(
            tmp, "fnack.resolver-like", '["track.resolve"]', "ResolverPlugin",
        )
        (resolver_like / "plugin.py").write_text(
            "from plugins.base import ServerExtensionPlugin\n"
            "class ResolverPlugin(ServerExtensionPlugin):\n"
            "    def register_routes(self, blueprint): pass\n"
        )

        mgr = init_plugin_manager(
            plugins_dir=str(tmp),
            bundled_plugins_dir=None,
            core_version="0.3.1",
        )
        mgr.load_all()  # enables everything discovered

        # Declared capabilities are what the manager registers.
        assert set(mgr.get_plugin_capabilities("fnack.navidrome-like")) == {
            MEDIA_SCAN, MEDIA_HEALTH, MEDIA_CONNECTION_TEST, SERVER_EXTENSION,
        }
        assert mgr.get_plugin_capabilities("fnack.resolver-like") == [TRACK_RESOLVE]

        reg: CapabilityRegistry = mgr.capability_registry
        assert reg.has(MEDIA_SCAN)
        assert reg.has(MEDIA_HEALTH)
        assert reg.has(MEDIA_CONNECTION_TEST)
        assert reg.has(SERVER_EXTENSION)
        assert reg.has(TRACK_RESOLVE)
        # Each capability resolves to the declaring plugin.
        assert [h.plugin_id for h in reg.providers(MEDIA_SCAN)] == ["fnack.navidrome-like"]
        assert [h.plugin_id for h in reg.providers(TRACK_RESOLVE)] == ["fnack.resolver-like"]
        # The navidrome-like plugin serves 4 capabilities from ONE handle.
        assert reg.capabilities_for("fnack.navidrome-like") == sorted({
            MEDIA_SCAN, MEDIA_HEALTH, MEDIA_CONNECTION_TEST, SERVER_EXTENSION,
        })


def test_type_derivation_when_manifest_omits_capabilities() -> None:
    """Manifests without `capabilities` derive them from `type` (downloader ->
    download.track, etc.) so third-party plugins still register sensibly."""
    import tempfile
    from plugins.base import DownloaderPlugin, DownloadResult, TrackRef
    from plugins.manager import init_plugin_manager
    from pathlib import Path as _P

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


def test_disable_removes_capabilities() -> None:
    """MASTER rule 2: disabling a plugin makes its capability disappear —
    no hidden fallback."""
    import tempfile
    from plugins.manager import init_plugin_manager

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        pdir = _make_plugin_dir(tmp, "fnack.disable-me", '["track.resolve"]', "DisableMe")
        (pdir / "plugin.py").write_text(
            "from plugins.base import ServerExtensionPlugin\n"
            "class DisableMe(ServerExtensionPlugin):\n"
            "    def register_routes(self, blueprint): pass\n"
        )
        mgr = init_plugin_manager(plugins_dir=str(tmp), bundled_plugins_dir=None, core_version="0.3.1")
        mgr.load_all()
        assert mgr.capability_registry.has(TRACK_RESOLVE)

        mgr.disable_plugin("fnack.disable-me")
        assert mgr.capability_registry.has(TRACK_RESOLVE) is False
        assert mgr.capability_registry.providers(TRACK_RESOLVE) == []

        # Re-enable brings it back.
        mgr.enable_plugin("fnack.disable-me")
        assert mgr.capability_registry.has(TRACK_RESOLVE) is True


def test_priority_override_flows_into_registry() -> None:
    """The user-facing priority override must reorder capability providers
    (priorities remain core)."""
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
        # Override: make fnack.second the first provider.
        loaded = mgr.get_loaded("fnack.second")
        loaded.priority_override = 1
        mgr.refresh_capability_registration("fnack.second")
        assert [h.plugin_id for h in mgr.capability_registry.providers("download.track")] == [
            "fnack.second", "fnack.first",
        ]


if __name__ == "__main__":
    test_manifest_declares_multiple_capabilities()
    test_type_derivation_when_manifest_omits_capabilities()
    test_disable_removes_capabilities()
    test_priority_override_flows_into_registry()
    print("test_multi_capability_plugins: PASSED")
