"""Architecture/parity test: yt-dlp provider extraction (Phase 2, PR 4).

Verifies the Phase 2 extraction contract for fnack.ytdlp:

1. The provider implementation lives in the plugin (`ytdlp.py`), NOT in
   `services/` — core no longer imports `services.ytdlp_service` and the
   service file is gone (along with the legacy `spotdl_service` alias).
2. The plugin implements the FINAL SDK `TrackDownloader` contract
   (request-object based, async), owns its settings, and exposes cookies
   helpers through the manager boundary (no direct core->service import).
3. The queue chain's migration adapter invokes ytdlp with a DownloadRequest
   carrying the provider-neutral hints (query/cookies/check_duration) and
   normalizes the SDK DownloadResult to the legacy shape.
4. The manual-download path routes raw query/URLs through the provider
   boundary, not a direct core->service call.
5. The cookies settings UI routes through the provider (duck-typed), with a
   core fallback.

Run from the repo root:

    .venv/bin/python tests/architecture/test_ytdlp_extraction.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_provider_impl_lives_in_plugin_not_core() -> None:
    """The provider code moved into bundled_plugins/fnack.ytdlp/ytdlp.py; the
    old core services are deleted; no core file imports them."""
    plugin_module = ROOT / "bundled_plugins" / "fnack.ytdlp" / "ytdlp.py"
    assert plugin_module.exists(), "plugin-owned ytdlp.py must exist"
    assert not (ROOT / "services" / "ytdlp_service.py").exists(), \
        "services/ytdlp_service.py must be deleted"
    assert not (ROOT / "services" / "spotdl_service.py").exists(), \
        "services/spotdl_service.py (legacy alias) must be deleted"

    for py in [ROOT / "app.py", *(ROOT / "services").glob("*.py")]:
        text = py.read_text(encoding="utf-8")
        assert "ytdlp_service" not in text, f"{py.name} still imports the deleted service"
        assert "spotdl_service" not in text, f"{py.name} still imports the deleted spotdl alias"


def test_plugin_implements_sdk_downloader_contract() -> None:
    """The plugin is a TrackDownloader (FINAL contract) AND a PluginBase, and
    exposes cookies helpers for the settings UI."""
    from fnack.plugin_api.providers import TrackDownloader
    from plugins.base import PluginBase

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fnack_plugin_fnack_ytdlp",
        ROOT / "bundled_plugins" / "fnack.ytdlp" / "plugin.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    plugin_dir = str(ROOT / "bundled_plugins" / "fnack.ytdlp")
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
    cls = module.YtDlpDownloader
    assert issubclass(cls, PluginBase), "entry point must subclass PluginBase"
    instance = object.__new__(cls)
    assert isinstance(instance, TrackDownloader), "must implement the SDK TrackDownloader protocol"
    assert cls.capability_id == "download.track"
    import inspect
    assert inspect.iscoroutinefunction(cls.download), "download() must be async (SDK contract)"
    assert inspect.iscoroutinefunction(cls.can_handle), "can_handle() must be async (SDK contract)"
    assert hasattr(cls, "get_cookies_status"), "cookies status helper must exist on the plugin"
    assert hasattr(cls, "get_cookies_path"), "cookies path helper must exist on the plugin"
    assert hasattr(cls, "on_load"), "lifecycle hook present"


def test_migration_adapter_passes_hints_and_normalizes() -> None:
    """The adapter builds a DownloadRequest carrying query/cookies/
    check_duration and normalizes the SDK DownloadResult to the legacy shape."""
    from services.queue_service import (
        _build_download_request,
        _invoke_downloader_can_handle,
        _invoke_downloader_download,
    )
    from fnack.plugin_api.models import DownloadResult as SdkResult
    from plugins.base import TrackRef
    from pathlib import Path as _P

    class SdkYtdlp:
        capability_id = "download.track"
        async def can_handle(self, request):
            return bool(request and (request.track or request.query))
        async def download(self, request):
            # Verify the hints flowed through.
            assert request.query == "https://youtu.be/xyz"
            assert request.cookies_path == "/tmp/c.txt"
            assert request.check_duration is False
            return SdkResult(provider_id="fnack.ytdlp", success=True,
                             path=_P("/tmp/out.opus"), message=None, retryable=False)

    class FakeManager:
        def invoke_provider(self, provider, method_name, *args, timeout=None, **kwargs):
            method = getattr(provider, method_name)
            import inspect
            r = method(*args, **kwargs)
            if inspect.isawaitable(r):
                import asyncio
                return asyncio.run(r)
            return r

    fm = FakeManager()
    provider = SdkYtdlp()
    tr = TrackRef(id=1, title="T", artist_name="A", album_name="B")
    opts = {"query": "https://youtu.be/xyz", "cookies_path": "/tmp/c.txt",
            "check_duration": False, "format": "opus"}
    assert _invoke_downloader_can_handle(fm, provider, tr, _P("/tmp"), opts) is True
    result = _invoke_downloader_download(fm, provider, tr, _P("/tmp"), opts, 10)
    assert result.success is True and result.file_path == _P("/tmp/out.opus")
    assert result.source_plugin_id == "fnack.ytdlp"
    # _build_download_request carries the hints into an SDK request.
    req = _build_download_request(tr, _P("/tmp"), opts)
    assert req.query == "https://youtu.be/xyz" and req.check_duration is False


def test_manual_download_routes_through_provider() -> None:
    """The manual path invokes download.track providers (ytdlp for raw
    queries) via the guarded boundary — no direct core->service call remains."""
    from services import queue_service as qs
    assert hasattr(qs, "_download_via_ytdlp_provider")
    src = Path(qs.__file__).read_text(encoding="utf-8")
    assert "download_track_ytdlp(" not in src, "manual path must not call the service directly"
    assert "ytdlp_service" not in src, "queue_service must not import the deleted service"
    assert "spotdl_service" not in src, "queue_service must not import the deleted spotdl alias"


def test_cookies_ui_routes_through_provider() -> None:
    """app.py's cookies routes use the provider (duck-typed), not a direct
    services import; a core fallback keeps the page working."""
    src = Path(ROOT / "app.py").read_text(encoding="utf-8")
    assert "ytdlp_service" not in src, "app.py must not import the deleted service"
    # No module-level import of the cookies helpers from the deleted service;
    # routes go through _cookies_status/_cookies_path wrappers.
    assert "from services.ytdlp_service import" not in src
    # The wrappers themselves may call provider.get_cookies_status — that is
    # the intended manager boundary; just ensure the ROUTES use the wrappers.
    assert "_cookies_status(" in src and "_cookies_path(" in src
    # The wrappers must exist; source-level assertion is the check (executing
    # app.py would boot the whole app).


if __name__ == "__main__":
    test_provider_impl_lives_in_plugin_not_core()
    test_plugin_implements_sdk_downloader_contract()
    test_migration_adapter_passes_hints_and_normalizes()
    test_manual_download_routes_through_provider()
    test_cookies_ui_routes_through_provider()
    print("test_ytdlp_extraction: PASSED")
