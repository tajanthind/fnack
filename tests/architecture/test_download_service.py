"""Architecture/parity test: DownloadService (Phase 3, Step 1).

Verifies the Phase 3 application-service contract for download.track:

1. DownloadService resolves download.track providers via the capability
   registry (priority order) — it never names a provider and the queue never
   iterates the registry itself.
2. Zero enabled providers -> CapabilityUnavailable("download.track",
   "download_track") — a structured result, no hidden fallback (MASTER rule 3).
3. Sequential fallback: a provider that fails (download error) is skipped and
   the next provider is tried.
4. The optional `verify` hook implements the per-provider verification policy:
   accept returns the result, reject tries the next provider, flag attaches
   caution info to result.metadata.
5. `stop_on_first_attempt` (manual path) returns after the first provider
   that passes can_handle, success or failure.
6. The queue uses DownloadService — queue_service.py no longer contains the
   provider-invocation loop/adapter (it delegates to the service).

Run from the repo root:

    .venv/bin/python tests/architecture/test_download_service.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _sdk_result(provider_id, success=True, path="/tmp/out.flac", message=None):
    from fnack.plugin_api.models import DownloadResult
    return DownloadResult(
        provider_id=provider_id, success=success,
        path=Path(path) if path is not None else None,
        message=message, retryable=True,
    )


def _fake_manager(*providers):
    """Minimal manager stand-in: get_downloaders() returns the providers;
    invoke_provider consumes timeout and calls the method directly (mirrors
    PluginManager.invoke_provider)."""
    import asyncio
    import inspect

    class _FakeManager:
        def __init__(self):
            self._providers = list(providers)

        def get_downloaders(self):
            return self._providers

        def invoke_provider(self, provider, method_name, *args, timeout=None, **kwargs):
            method = getattr(provider, method_name, None)
            if method is None:
                return None
            r = method(*args, **kwargs)
            if inspect.isawaitable(r):
                return asyncio.run(r)
            return r

    return _FakeManager()


class _SdkProvider:
    """SDK-contract provider stub: can_handle by attribute, download returns a
    canned SDK result."""

    def __init__(self, provider_id, priority, can_handle=True, result=None, rate_limited=False):
        self._pid = provider_id
        self.priority = priority
        self._can = can_handle
        self._result = result
        self._limited = rate_limited

    capability_id = "download.track"

    @property
    def manifest(self):
        class _M:
            id = self._pid
            name = self._pid
        return _M()

    async def can_handle(self, request):
        return self._can

    async def download(self, request):
        if self._result is not None:
            return self._result
        return _sdk_result(self._pid)

    def is_rate_limited(self):
        return self._limited


def test_resolves_providers_via_capability_registry() -> None:
    """DownloadService uses the manager's get_downloaders() (capability
    registry, priority-ordered) — never a provider-ID branch."""
    from services.download_service import DownloadService

    p1 = _SdkProvider("fnack.spotiflac", 10)
    p2 = _SdkProvider("fnack.ytdlp", 50)
    svc = DownloadService(manager=_fake_manager(p1, p2))
    assert svc.resolve_providers() == [p1, p2]

    # Source-level: the service has no provider-ID keys; the queue delegates.
    src = (ROOT / "services" / "download_service.py").read_text(encoding="utf-8")
    for needle in ['"fnack.spotiflac"', '"fnack.ytdlp"', '"fnack.deezer-batch"']:
        assert needle not in src, f"DownloadService must not name a provider ({needle})"


def test_zero_providers_raises_capability_unavailable() -> None:
    """Zero enabled providers -> structured CapabilityUnavailable, raised by
    the service (not a hidden fallback)."""
    from services.download_service import CapabilityUnavailable, DownloadService
    from fnack.plugin_api.models import DownloadRequest
    from plugins.base import TrackRef

    svc = DownloadService(manager=_fake_manager())
    req = DownloadRequest(track=TrackRef(id=1, title="T", artist_name="A", album_name="B"),
                          destination=Path("/tmp"))
    try:
        svc.download(req)
    except CapabilityUnavailable as e:
        assert e.capability == "download.track"
        assert e.operation == "download_track"
    else:
        raise AssertionError("expected CapabilityUnavailable with zero providers")


def test_sequential_fallback_tries_next_provider_on_failure() -> None:
    """A provider that fails to produce a file is skipped; the next provider
    is tried and its result returned."""
    from services.download_service import DownloadService
    from fnack.plugin_api.models import DownloadRequest
    from plugins.base import TrackRef

    fail = _SdkProvider("fnack.spotiflac", 10,
                        result=_sdk_result("fnack.spotiflac", success=False, path=None, message="boom"))
    ok = _SdkProvider("fnack.ytdlp", 50)
    svc = DownloadService(manager=_fake_manager(fail, ok))
    req = DownloadRequest(track=TrackRef(id=1, title="T", artist_name="A", album_name="B"),
                          destination=Path("/tmp"))
    result = svc.download(req)
    assert result.success is True
    assert result.provider_id == "fnack.ytdlp"
    assert result.path == Path("/tmp/out.flac")


def test_verify_hook_accept_flag_reject() -> None:
    """The verify hook is the per-provider verification policy: accept returns
    the result; reject records the reason and tries the next provider; flag
    attaches caution info to result.metadata."""
    from services.download_service import DownloadService, VerifyVerdict
    from fnack.plugin_api.models import DownloadRequest
    from plugins.base import TrackRef

    wrong = _SdkProvider("fnack.spotiflac", 10,
                         result=_sdk_result("fnack.spotiflac", path="/tmp/wrong.flac"))
    right = _SdkProvider("fnack.ytdlp", 50,
                         result=_sdk_result("fnack.ytdlp", path="/tmp/right.flac"))
    svc = DownloadService(manager=_fake_manager(wrong, right))

    def reject_first(result):
        if result.provider_id == "fnack.spotiflac":
            return VerifyVerdict("reject", error="wrong song")
        return VerifyVerdict("accept")

    req = DownloadRequest(track=TrackRef(id=1, title="T", artist_name="A", album_name="B"),
                          destination=Path("/tmp"))
    result = svc.download(req, verify=reject_first)
    assert result.success is True
    assert result.provider_id == "fnack.ytdlp"

    # Flag: first provider's file is kept but flagged.
    def flag_first(result):
        if result.provider_id == "fnack.spotiflac":
            return VerifyVerdict("flag", caution={"matched_title": "Other", "score": 0.4})
        return VerifyVerdict("accept")

    result2 = svc.download(req, verify=flag_first)
    assert result2.success is True
    assert result2.provider_id == "fnack.spotiflac"
    assert (result2.metadata or {}).get("caution", {}).get("matched_title") == "Other"
    # file_meta attached for the caller (queue finalize)
    assert "file_meta" in (result2.metadata or {})


def test_stop_on_first_attempt_returns_first_can_handle() -> None:
    """Manual path: stop_on_first_attempt returns after the first provider
    that passes can_handle — success OR failure (the caller chains fallbacks
    explicitly)."""
    from services.download_service import DownloadService
    from fnack.plugin_api.models import DownloadRequest
    from plugins.base import TrackRef

    fail = _SdkProvider("fnack.spotiflac", 10, can_handle=True,
                        result=_sdk_result("fnack.spotiflac", success=False, path=None, message="nope"))
    would_try = _SdkProvider("fnack.ytdlp", 50, can_handle=False)
    svc = DownloadService(manager=_fake_manager(fail, would_try))
    req = DownloadRequest(track=TrackRef(id=1, title="T", artist_name="A", album_name="B"),
                          destination=Path("/tmp"))
    result = svc.download(req, stop_on_first_attempt=True)
    assert result.success is False
    assert "fnack.spotiflac" in (result.message or "")


def test_queue_delegates_to_download_service() -> None:
    """Source-level: queue_service.py no longer contains the provider
    invocation loop/adapter — it calls DownloadService. No provider-ID gate,
    no get_downloaders iteration in the queue."""
    src = (ROOT / "services" / "queue_service.py").read_text(encoding="utf-8")
    assert "DownloadService" in src, "queue must delegate to DownloadService"
    for needle in ["_invoke_downloader_download(", "get_downloaders()", "engine_gates"]:
        assert needle not in src, f"queue_service.py must not iterate providers ({needle})"
    assert "from services.download_service import" in src


if __name__ == "__main__":
    test_resolves_providers_via_capability_registry()
    test_zero_providers_raises_capability_unavailable()
    test_sequential_fallback_tries_next_provider_on_failure()
    test_verify_hook_accept_flag_reject()
    test_stop_on_first_attempt_returns_first_can_handle()
    test_queue_delegates_to_download_service()
    print("test_download_service: PASSED")
