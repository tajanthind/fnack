"""Provider protocols + the ProviderExecutor.

Small, capability-oriented interfaces (PHASE 1 §Provider protocols). A
provider is any object that implements one-or-more of these protocols; the
registry doesn't care about concrete classes, only about `capability_id`.

Protocols are declared async (the executor supports both sync and async
methods — `runtime_checkable` only checks member presence, so a synchronous
implementation of the same method names still satisfies the protocol).

ProviderExecutor is the ONE place that drives awaitables — never scatter
`asyncio.run()` through providers (MASTER §Async providers).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Optional, Protocol, runtime_checkable

from fnack.plugin_api.capabilities import (
    AUTH_PROVIDER,
    DOWNLOAD_TRACK,
    FINGERPRINT_IDENTIFY,
    LIBRARY_TASK,
    MEDIA_SCAN,
    NETWORK_ROUTE,
    NOTIFICATION_EVENT,
    SERVER_EXTENSION,
    TRACK_RESOLVE,
)
from fnack.plugin_api.errors import ProviderError
from fnack.plugin_api.models import (
    DownloadRequest,
    DownloadResult,
    FingerprintEvidence,
    FingerprintRequest,
    TaskResult,
    TrackCandidate,
    TrackResolveRequest,
)


# -- download ---------------------------------------------------------------

@runtime_checkable
class TrackDownloader(Protocol):
    capability_id = DOWNLOAD_TRACK

    async def can_handle(self, request: DownloadRequest) -> bool: ...
    async def download(self, request: DownloadRequest) -> DownloadResult: ...


# -- resolution / metadata --------------------------------------------------

@runtime_checkable
class TrackResolver(Protocol):
    capability_id = TRACK_RESOLVE

    async def resolve(self, request: TrackResolveRequest) -> list[TrackCandidate]: ...


# -- fingerprint ------------------------------------------------------------

@runtime_checkable
class FingerprintProvider(Protocol):
    capability_id = FINGERPRINT_IDENTIFY

    async def identify(self, request: FingerprintRequest) -> FingerprintEvidence: ...


# -- media server -----------------------------------------------------------

@runtime_checkable
class MediaScanner(Protocol):
    capability_id = MEDIA_SCAN

    async def trigger_scan(self) -> tuple[bool, str]: ...
    async def test_connection(self) -> tuple[bool, str]: ...


# -- library tasks ----------------------------------------------------------

@runtime_checkable
class LibraryTaskProvider(Protocol):
    capability_id = LIBRARY_TASK

    async def run(self) -> TaskResult: ...


# -- server extensions ------------------------------------------------------

@runtime_checkable
class ServerExtension(Protocol):
    capability_id = SERVER_EXTENSION

    async def register_routes(self, blueprint: object) -> None: ...


# -- auth -------------------------------------------------------------------

@runtime_checkable
class AuthProvider(Protocol):
    capability_id = AUTH_PROVIDER

    async def authenticate(self, request_headers: dict) -> Optional[str]: ...


# -- notifications ----------------------------------------------------------

@runtime_checkable
class NotificationProvider(Protocol):
    capability_id = NOTIFICATION_EVENT

    async def notify(self, event_name: str, payload: dict) -> None: ...


# -- network ----------------------------------------------------------------

@runtime_checkable
class NetworkRouter(Protocol):
    capability_id = NETWORK_ROUTE

    async def start(self) -> tuple[bool, str]: ...
    async def stop(self) -> tuple[bool, str]: ...
    async def status(self) -> dict: ...


# -- executor ---------------------------------------------------------------

class ProviderExecutor:
    """Detects awaitables centrally and drives them to completion.

    - sync method  -> call it, return its value
    - async method -> await it (optionally with a timeout via asyncio.wait_for)
    - `run`        -> sync entry point for gevent callers: if the provider
      method returns an awaitable, drive it with asyncio.run() HERE (the one
      sanctioned place), never in provider code.
    """

    async def invoke(
        self,
        provider: object,
        method_name: str,
        *args,
        timeout: Optional[float] = None,
        **kwargs,
    ):
        method = getattr(provider, method_name, None)
        if method is None or not callable(method):
            raise ProviderError(
                provider_id=getattr(provider, "manifest", None).id if getattr(provider, "manifest", None) else str(provider),
                code="no_such_method",
                message=f"provider has no method '{method_name}'",
            )
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            if timeout is not None:
                result = await asyncio.wait_for(result, timeout)
            else:
                result = await result
        return result

    def run(
        self,
        provider: object,
        method_name: str,
        *args,
        timeout: Optional[float] = None,
        **kwargs,
    ):
        """Synchronous wrapper of `invoke` for gevent-style callers.

        If the provider method is async, this is the single central place
        that drives the coroutine (asyncio.run) — providers must never do
        this themselves.
        """
        method = getattr(provider, method_name, None)
        if method is None or not callable(method):
            raise ProviderError(
                provider_id=getattr(provider, "manifest", None).id if getattr(provider, "manifest", None) else str(provider),
                code="no_such_method",
                message=f"provider has no method '{method_name}'",
            )
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            if timeout is not None:
                result = asyncio.run(asyncio.wait_for(result, timeout))
            else:
                result = asyncio.run(result)
        return result
