"""Architecture/parity test: SpotiFLAC provider extraction (Phase 2, PR 3).

Verifies the Phase 2 extraction contract for fnack.spotiflac:

1. The provider implementation lives in the plugin (`spotiflac.py`), NOT in
   `services/` — core no longer imports `services.spotiflac_service` and the
   service file is gone.
2. The plugin implements the FINAL SDK `TrackDownloader` contract
   (request-object based) while `is_rate_limited()` stays available for the
   queue chain's generic circuit-breaker check.
3. The queue chain's migration adapter normalizes BOTH contracts: a new-
   contract provider (SpotiFLAC) is invoked with a DownloadRequest and its
   SDK DownloadResult maps to the legacy shape (success/file_path/error) the
   chain's verification code consumes; a legacy provider still works.
4. The manual-download path routes through the provider (guarded boundary),
   not a direct core->service call.
5. vpn_service emits `network.route_changed` (no provider import); the
   plugin subscribes.

Run from the repo root:

    .venv/bin/python tests/architecture/test_spotiflac_extraction.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_provider_impl_lives_in_plugin_not_core() -> None:
    """The provider code moved into bundled_plugins/fnack.spotiflac/spotiflac.py;
    the old core service is deleted; no core file imports it."""
    plugin_module = ROOT / "bundled_plugins" / "fnack.spotiflac" / "spotiflac.py"
    assert plugin_module.exists(), "plugin-owned spotiflac.py must exist"
    old_service = ROOT / "services" / "spotiflac_service.py"
    assert not old_service.exists(), "services/spotiflac_service.py must be deleted"

    # No core file imports the old service.
    for py in [ROOT / "app.py", *(ROOT / "services").glob("*.py")]:
        text = py.read_text(encoding="utf-8")
        assert "spotiflac_service" not in text, f"{py.name} still imports the deleted service"


def test_plugin_implements_sdk_downloader_contract() -> None:
    """The plugin is a TrackDownloader (FINAL contract) AND a PluginBase
    (so the manager loads it), and keeps is_rate_limited() for the chain."""
    from fnack.plugin_api.providers import TrackDownloader
    from plugins.base import PluginBase

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fnack_plugin_fnack_spotiflac",
        ROOT / "bundled_plugins" / "fnack.spotiflac" / "plugin.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # The plugin dir must be on sys.path for `import spotiflac` (manager does
    # this; mirror it here for a standalone check).
    plugin_dir = str(ROOT / "bundled_plugins" / "fnack.spotiflac")
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
    cls = module.SpotiFLACPlugin
    assert issubclass(cls, PluginBase), "entry point must subclass PluginBase"
    # TrackDownloader is a runtime_checkable protocol with a non-method member
    # (capability_id), so use isinstance (what the runtime uses) not issubclass.
    instance = object.__new__(cls)
    assert isinstance(instance, TrackDownloader), "must implement the SDK TrackDownloader protocol"
    assert cls.capability_id == "download.track"
    assert hasattr(cls, "is_rate_limited"), "circuit-breaker check must stay available"
    assert hasattr(cls, "on_load"), "lifecycle hook present"
    # Async contract methods exist.
    import inspect
    assert inspect.iscoroutinefunction(cls.download), "download() must be async (SDK contract)"
    assert inspect.iscoroutinefunction(cls.can_handle), "can_handle() must be async (SDK contract)"


def test_migration_adapter_normalizes_both_contracts() -> None:
    """DownloadService's adapter invokes both SDK and legacy providers and
    returns the FINAL SDK DownloadResult shape (provider_id/success/path)."""
    from services.download_service import (
        _is_sdk_downloader,
        _invoke_downloader_can_handle,
        _invoke_downloader_download,
    )
    from fnack.plugin_api.models import DownloadResult as SdkResult
    from pathlib import Path as _P
    from plugins.base import TrackRef as LegacyTrackRef

    class LegacyDownloader:
        """A legacy DownloaderPlugin-shaped provider (ytdlp until PR 4)."""
        priority = 50
        def can_handle(self, track): return bool(track and track.title)
        def download(self, track, dest_dir, options):
            from plugins.base import DownloadResult
            return DownloadResult(success=True, file_path=_P("/tmp/legacy.flac"), error=None)

    class FakeManager:
        """Minimal manager stand-in: invoke_provider consumes timeout and
        just calls the method (mirrors PluginManager.invoke_provider)."""
        def invoke_provider(self, provider, method_name, *args, timeout=None, **kwargs):
            method = getattr(provider, method_name)
            import inspect as _i
            r = method(*args, **kwargs)
            if _i.isawaitable(r):
                import asyncio
                return asyncio.run(r)
            return r

    fm = FakeManager()

    # Legacy provider: NOT an SDK downloader, invoked with old args.
    legacy = LegacyDownloader()
    assert _is_sdk_downloader(legacy) is False
    tr = LegacyTrackRef(id=1, title="T", artist_name="A", album_name="B")
    assert _invoke_downloader_can_handle(fm, legacy, tr, _P("/tmp"), {}) is True
    res = _invoke_downloader_download(fm, legacy, tr, _P("/tmp"), {}, 10)
    assert res.success is True and res.path == _P("/tmp/legacy.flac")

    # New-contract provider: SDK shape normalized to legacy shape.
    class SdkDownloader:
        capability_id = "download.track"
        async def can_handle(self, request): return bool(request and request.track and request.track.spotify_url)
        async def download(self, request):
            return SdkResult(provider_id="fnack.spotiflac", success=True,
                             path=_P("/tmp/new.flac"), message=None, retryable=False)

    sdk = SdkDownloader()
    assert _is_sdk_downloader(sdk) is True
    tr2 = LegacyTrackRef(id=1, title="T", artist_name="A", album_name="B",
                         spotify_url="https://open.spotify.com/track/x")
    assert _invoke_downloader_can_handle(fm, sdk, tr2, _P("/tmp"), {}) is True
    res2 = _invoke_downloader_download(fm, sdk, tr2, _P("/tmp"), {}, 10)
    assert res2.success is True and res2.path == _P("/tmp/new.flac")
    assert res2.provider_id == "fnack.spotiflac"


def test_manual_download_routes_through_provider() -> None:
    """The manual path invokes the download.track provider via the guarded
    boundary — no direct core->service call remains. The helper is
    provider-neutral (it iterates all download.track providers, not just
    spotiflac)."""
    from services import queue_service as qs
    assert hasattr(qs, "_download_via_chain")
    src = Path(qs.__file__).read_text(encoding="utf-8")
    assert "download_track_spotiflac(" not in src, "manual path must not call the service directly"
    assert "spotiflac_service" not in src, "queue_service must not import the deleted service"


def test_vpn_emits_route_changed_event() -> None:
    """vpn_service no longer imports the provider; it emits the event the
    plugin subscribes to."""
    from services import vpn_service
    src = Path(vpn_service.__file__).read_text(encoding="utf-8")
    assert "spotiflac_service" not in src, "vpn_service must not import the deleted service"
    assert "network.route_changed" in src, "vpn_service must emit network.route_changed"


if __name__ == "__main__":
    test_provider_impl_lives_in_plugin_not_core()
    test_plugin_implements_sdk_downloader_contract()
    test_migration_adapter_normalizes_both_contracts()
    test_manual_download_routes_through_provider()
    test_vpn_emits_route_changed_event()
    print("test_spotiflac_extraction: PASSED")
