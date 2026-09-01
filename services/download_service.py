"""DownloadService — application service for the download.track capability.

Phase 3 (brief 03): the queue orchestrates; this service owns the download
policy. It resolves `download.track` providers via the capability registry
(enabled, priority-ordered), applies the policy (rate-limit skip, can_handle
gate, sequential fallback, optional per-provider verification feedback), and
invokes providers through the manager's ProviderExecutor boundary. Core never
names a provider and never iterates the registry itself.

Contract:
- zero enabled providers -> raises CapabilityUnavailable("download.track",
  "download_track") — missing capability is a valid state (MASTER rule 3);
  there is NO hidden provider fallback.
- providers exist but all fail -> returns a failure DownloadResult with the
  aggregated reasons; the queue records them (provider errors never crash the
  queue).
- `verify` (optional) is the download-policy hook: after each provider
  produces a file, the hook decides accept / flag (keep + caution) / reject
  (try the next provider). This preserves the chain's per-provider
  verify-and-fall-through semantics; a later phase swaps the hook for
  VerificationService.

The adapter helpers (`_is_sdk_downloader`, `_build_download_request`,
`_invoke_downloader_can_handle`, `_invoke_downloader_download`,
`_legacy_result`) moved here from queue_service — they are the service's
invocation machinery, not queue concern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fnack.plugin_api.capabilities import DOWNLOAD_TRACK
from fnack.plugin_api.errors import CapabilityUnavailable
from fnack.plugin_api.models import DownloadRequest, DownloadResult


# ---------------------------------------------------------------------------
# Provider-invocation adapter (moved from queue_service, Phase 2 PRs 3+4)
#
# SDK-contract providers (TrackDownloader: request-object based, async) are
# invoked with a DownloadRequest; providers that predate the SDK contract keep
# the legacy signature (track, work_dir, options). The adapter picks per
# provider and normalizes to the shapes below.
# ---------------------------------------------------------------------------

def _is_sdk_downloader(provider) -> bool:
    from fnack.plugin_api.providers import TrackDownloader
    try:
        return isinstance(provider, TrackDownloader)
    except TypeError:
        return False


def _build_download_request(track_ref, tmp_work_dir, options):
    """Build an SDK DownloadRequest from the chain's (track, work_dir, options)
    shape, carrying the provider-neutral hints (query/cookies/check_duration)
    the ytdlp provider needs."""
    return DownloadRequest(
        track=track_ref,
        destination=tmp_work_dir,
        quality=options.get("quality"),
        format=options.get("format"),
        query=options.get("query"),
        cookies_path=options.get("cookies_path"),
        audio_source=options.get("audio_source"),
        check_duration=bool(options.get("check_duration", True)),
    )


def _invoke_downloader_can_handle(pm, provider, track_ref, tmp_work_dir, options) -> bool:
    """Call can_handle through the guarded boundary, adapting the contract."""
    if _is_sdk_downloader(provider):
        request = _build_download_request(track_ref, tmp_work_dir, options)
        return bool(pm.invoke_provider(provider, "can_handle", request))
    return bool(pm.invoke_provider(provider, "can_handle", track_ref))


def _legacy_result(success, file_path=None, error=None, source_plugin_id=None, extra=None):
    from plugins.base import DownloadResult as _LegacyResult
    return _LegacyResult(
        success=bool(success),
        file_path=file_path,
        error=error,
        source_plugin_id=source_plugin_id,
        extra=extra or {},
    )


def _to_sdk_result(provider, legacy) -> Optional[DownloadResult]:
    """Convert a legacy-shaped result (success/file_path/error) to the SDK
    DownloadResult shape the service returns. None when the provider produced
    no result at all."""
    if legacy is None:
        return None
    manifest = getattr(provider, "manifest", None)
    return DownloadResult(
        provider_id=getattr(manifest, "id", "") if manifest else "",
        success=bool(getattr(legacy, "success", False)),
        path=getattr(legacy, "file_path", None),
        error_code=None,
        message=getattr(legacy, "error", None),
        retryable=True,
        metadata=dict(getattr(legacy, "extra", {}) or {}),
    )


def _invoke_downloader_download(pm, provider, track_ref, tmp_work_dir, options,
                                timeout: float) -> DownloadResult:
    """Invoke download through the guarded boundary; always returns the SDK
    DownloadResult shape (legacy providers are normalized up)."""
    if _is_sdk_downloader(provider):
        request = _build_download_request(track_ref, tmp_work_dir, options)
        result = pm.invoke_provider(provider, "download", request, timeout=timeout)
        if result is None:
            return None
        return result  # already the SDK DownloadResult
    result = pm.invoke_provider(provider, "download", track_ref, tmp_work_dir, options,
                                timeout=timeout)
    return _to_sdk_result(provider, result)


# ---------------------------------------------------------------------------
# DownloadService
# ---------------------------------------------------------------------------

@dataclass
class VerifyVerdict:
    """Download-policy verdict returned by the optional `verify` hook.

    - status "accept" -> file accepted, chain stops.
    - status "flag"   -> file kept but flagged for the user (caution info);
                         chain stops (caller emits the caution event).
    - status "reject" -> file rejected; the service tries the next provider.
    """
    status: str  # "accept" | "flag" | "reject"
    meta: dict = field(default_factory=dict)          # e.g. bitrate/format
    caution: Optional[dict] = None                     # flagged-different-song info
    error: Optional[str] = None                        # rejection reason


class DownloadService:
    """Owns download.track resolution + the download policy.

    Construct with an explicit manager for tests; defaults to the process-wide
    plugin_manager.
    """

    def __init__(self, manager=None):
        self._manager = manager

    # -- resolution ---------------------------------------------------------

    def _pm(self):
        if self._manager is not None:
            return self._manager
        from plugins.manager import plugin_manager
        return plugin_manager

    def resolve_providers(self) -> list:
        """Enabled download.track providers, priority-ordered (capability
        registry). Empty when no enabled plugin supplies the capability."""
        pm = self._pm()
        if pm is None:
            return []
        return pm.get_downloaders()

    # -- policy helpers -----------------------------------------------------

    @staticmethod
    def _provider_options(provider, request: DownloadRequest) -> dict:
        """Per-provider options: plugin settings authoritative, request hints
        (the queue's legacy fallbacks) as fallback — same precedence as the
        pre-service chain."""
        opts: dict[str, Any] = {
            "query": request.query,
            "check_duration": request.check_duration,
        }
        try:
            settings = getattr(provider, "context", None)
            get = settings.get if settings is not None else (lambda k, d=None: d)
            opts["quality"] = get("quality") or request.quality
            opts["format"] = get("format") or request.format
            opts["audio_source"] = get("audio_source") or request.audio_source
            opts["cookies_path"] = get("cookies_path") or request.cookies_path
        except Exception:
            opts["quality"] = request.quality
            opts["format"] = request.format
            opts["audio_source"] = request.audio_source
            opts["cookies_path"] = request.cookies_path
        return opts

    # -- the download policy ------------------------------------------------

    def download(
        self,
        request: DownloadRequest,
        *,
        verify: Optional[Callable[[DownloadResult], VerifyVerdict]] = None,
        on_progress: Optional[Callable[[int, object], None]] = None,
        stop_on_first_attempt: bool = False,
    ) -> DownloadResult:
        """Resolve download.track and try providers in priority order.

        - zero providers -> CapabilityUnavailable("download.track",
          "download_track").
        - each provider: rate-limit skip -> can_handle gate -> download (with
          the download timeout) through the manager boundary.
        - `verify` (optional): called with the provider's SDK result when a
          file was produced. "accept"/"flag" return that result (flag info in
          result.metadata["caution"]); "reject" records the reason and tries
          the next provider.
        - `on_progress(idx, provider)`: called before each provider attempt
          (the queue emits progress events + logs from it).
        - `stop_on_first_attempt` (manual path): return after the first
          provider that passes can_handle — success OR failure — preserving
          the pre-service manual-path semantics (the caller chains fallbacks
          explicitly). Default False = sequential fallback.
        - all providers failed -> DownloadResult(success=False) with the
          aggregated reasons (never raises for provider failures).
        """
        pm = self._pm()
        providers = pm.get_downloaders() if pm is not None else []
        if not providers:
            raise CapabilityUnavailable(DOWNLOAD_TRACK, "download_track")

        reasons: list[str] = []
        for idx, provider in enumerate(providers):
            manifest = getattr(provider, "manifest", None)
            name = getattr(manifest, "name", None) or getattr(manifest, "id", "provider")

            if pm.invoke_provider(provider, "is_rate_limited"):
                reasons.append(f"{name} skipped (upstream rate limit circuit breaker)")
                if stop_on_first_attempt:
                    break
                continue

            options = self._provider_options(provider, request)
            if not _invoke_downloader_can_handle(pm, provider, request.track,
                                                 request.destination, options):
                continue

            # The provider will actually be attempted now (gates passed).
            if on_progress is not None:
                on_progress(idx, provider)

            from plugins.manager import DOWNLOAD_HOOK_TIMEOUT
            result = _invoke_downloader_download(
                pm, provider, request.track, request.destination, options,
                timeout=DOWNLOAD_HOOK_TIMEOUT)
            if result is not None and result.success and result.path:
                if verify is not None:
                    verdict = verify(result)
                    if verdict.status == "reject":
                        reason = verdict.error or "verification failed"
                        reasons.append(f"{name} verification failed: {reason}")
                        if stop_on_first_attempt:
                            break
                        continue
                    # accept / flag — attach verification output for the caller
                    meta = dict(result.metadata or {})
                    meta["file_meta"] = verdict.meta
                    if verdict.caution is not None:
                        meta["caution"] = verdict.caution
                    result.metadata = meta
                return result
            reason = (result.message if result is not None else "download returned no result")
            reasons.append(f"{name} failed: {reason}")
            if stop_on_first_attempt:
                break

        return DownloadResult(
            provider_id="",
            success=False,
            error_code="all_providers_failed",
            message="; ".join(reasons),
            retryable=True,
        )
