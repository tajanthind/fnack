"""Architecture test: async provider invocation (Phase 1, MASTER §Async
providers + PHASE 1 §Async provider executor).

ProviderExecutor is the ONE place that detects awaitables and drives them —
never `asyncio.run()` scattered through providers. Both sync and async
provider methods must work; timeouts apply to awaitables.

Run from the repo root:

    .venv/bin/python tests/architecture/test_async_provider_invocation.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from fnack.plugin_api.errors import ProviderError
from fnack.plugin_api.providers import ProviderExecutor


class SyncProvider:
    """A classic fnack provider: plain sync methods."""

    def can_handle(self, request) -> bool:
        return True

    def download(self, request) -> str:
        return "downloaded-sync"


class AsyncProvider:
    """A modern provider: async methods (awaitables)."""

    async def can_handle(self, request) -> bool:
        await asyncio.sleep(0)
        return True

    async def download(self, request) -> str:
        await asyncio.sleep(0)
        return "downloaded-async"

    async def slow(self) -> str:
        await asyncio.sleep(5)
        return "too-late"


class MixedProvider:
    """Sync method + async method on the same provider."""

    def can_handle(self, request) -> bool:
        return True

    async def resolve(self, request) -> list[str]:
        await asyncio.sleep(0)
        return ["candidate"]


def test_sync_provider_invoke() -> None:
    ex = ProviderExecutor()
    provider = SyncProvider()
    assert asyncio.run(ex.invoke(provider, "can_handle", None)) is True
    assert asyncio.run(ex.invoke(provider, "download", None)) == "downloaded-sync"


def test_async_provider_invoke() -> None:
    ex = ProviderExecutor()
    provider = AsyncProvider()
    assert asyncio.run(ex.invoke(provider, "can_handle", None)) is True
    assert asyncio.run(ex.invoke(provider, "download", None)) == "downloaded-async"


def test_mixed_provider() -> None:
    ex = ProviderExecutor()
    provider = MixedProvider()
    assert asyncio.run(ex.invoke(provider, "can_handle", None)) is True
    assert asyncio.run(ex.invoke(provider, "resolve", None)) == ["candidate"]


def test_sync_run_entrypoint() -> None:
    """`run` is the sync entrypoint for gevent-style callers; it drives
    awaitables centrally (the one sanctioned asyncio.run site)."""
    ex = ProviderExecutor()
    assert ex.run(SyncProvider(), "download", None) == "downloaded-sync"
    assert ex.run(AsyncProvider(), "download", None) == "downloaded-async"
    assert ex.run(MixedProvider(), "resolve", None) == ["candidate"]


def test_timeout_on_awaitable() -> None:
    ex = ProviderExecutor()
    provider = AsyncProvider()
    with _raises(asyncio.TimeoutError):
        asyncio.run(ex.invoke(provider, "slow", timeout=0.2))


def test_missing_method_raises_provider_error() -> None:
    ex = ProviderExecutor()
    provider = SyncProvider()
    try:
        asyncio.run(ex.invoke(provider, "does_not_exist"))
    except ProviderError as exc:
        assert exc.code == "no_such_method"
    else:
        raise AssertionError("expected ProviderError for missing method")
    try:
        ex.run(provider, "does_not_exist")
    except ProviderError as exc:
        assert exc.code == "no_such_method"
    else:
        raise AssertionError("expected ProviderError from run() for missing method")


class _raises:
    """Tiny context manager so the test has no pytest dependency."""

    def __init__(self, exc_type):
        self.exc_type = exc_type

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        assert exc_type is self.exc_type, f"expected {self.exc_type}, got {exc_type}: {exc}"
        return True


if __name__ == "__main__":
    test_sync_provider_invoke()
    test_async_provider_invoke()
    test_mixed_provider()
    test_sync_run_entrypoint()
    test_timeout_on_awaitable()
    test_missing_method_raises_provider_error()
    print("test_async_provider_invocation: PASSED")
