"""FingerprintService — application service for the fingerprint.identify capability.

Phase 3 (brief 03): discovers fingerprint providers via the capability
registry, invokes them (concurrently where practical) through the manager's
ProviderExecutor boundary, normalizes errors, enforces the timeout, and
returns normalized `FingerprintEvidence` for each provider.

Fingerprint semantics (brief §Fingerprint semantics + MASTER §Verification):

- provider no_match  -> no evidence (the provider is skipped; nothing to add)
- provider mismatch  -> negative evidence (FingerprintEvidence(status="mismatch"))
- provider timeout  -> provider error evidence
- provider unavailable -> CapabilityUnavailable (no enabled provider)

The service does NOT decide whether the download is valid — VerificationService
combines the evidence. A missing fingerprint result is never treated as proof
of a mismatch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fnack.plugin_api.capabilities import FINGERPRINT_IDENTIFY
from fnack.plugin_api.errors import CapabilityUnavailable
from fnack.plugin_api.models import FingerprintEvidence, FingerprintRequest


class FingerprintService:
    """Owns fingerprint.identify resolution + evidence normalization."""

    def __init__(self, manager=None):
        self._manager = manager  # injectable for tests

    # -- resolution ---------------------------------------------------------

    def _pm(self):
        if self._manager is not None:
            return self._manager
        from plugins.manager import plugin_manager
        return plugin_manager

    def _providers(self):
        """Enabled fingerprint.identify providers, priority-ordered."""
        pm = self._pm()
        if pm is None:
            raise CapabilityUnavailable(FINGERPRINT_IDENTIFY, "identify",
                                        "plugin manager not ready")
        if not pm.has_capability(FINGERPRINT_IDENTIFY):
            raise CapabilityUnavailable(FINGERPRINT_IDENTIFY, "identify")
        return pm.get_capability_providers(FINGERPRINT_IDENTIFY)

    # -- evidence normalization ---------------------------------------------

    @staticmethod
    def _normalize(provider, raw) -> Optional[FingerprintEvidence]:
        """Normalize whatever the provider returned into FingerprintEvidence.

        Providers implementing the SDK contract return FingerprintEvidence
        directly. Legacy providers (e.g. fnack.acoustid until its own
        extraction PR) return plugins.base.FingerprintResult (confidence /
        matched_title / matched_artist / raw). Both are normalized here.
        """
        if raw is None:
            return None
        manifest = getattr(provider, "manifest", None)
        provider_id = getattr(manifest, "id", "") if manifest else ""

        # SDK shape
        if isinstance(raw, FingerprintEvidence):
            return raw

        # Legacy FingerprintResult / duck-typed result
        confidence = getattr(raw, "confidence", None)
        title = getattr(raw, "matched_title", None)
        artist = getattr(raw, "matched_artist", None)
        raw_dict = dict(getattr(raw, "raw", {}) or {})
        # Provider no_match -> NO evidence (brief §Fingerprint semantics) —
        # a missing fingerprint result is never treated as a mismatch. A
        # zero-confidence/no-identity result is a no_match.
        if (confidence or 0) <= 0 and not title and not artist:
            return None
        return FingerprintEvidence(
            provider_id=provider_id,
            status="match",
            confidence=float(confidence) if confidence is not None else None,
            title=title,
            artist=artist,
            raw=raw_dict,
        )

    # -- identify -----------------------------------------------------------

    def identify(self, request: FingerprintRequest) -> list[FingerprintEvidence]:
        """Fingerprint a file with every enabled provider.

        Returns normalized evidence per provider; a provider that
        errors/timeouts yields an `error` evidence instead of crashing.
        Raises CapabilityUnavailable when no fingerprint provider is enabled.

        Providers run through the manager boundary (timeout + auto-disable
        guard). fnack's runtime is single-threaded gevent with the executor
        driving awaitables centrally, so providers run sequentially here —
        that satisfies the brief's "optionally execute providers
        concurrently" (concurrency becomes safe when the executor exposes an
        async fan-out; sequential is behavior-preserving today).
        """
        providers = self._providers()
        evidence: list[FingerprintEvidence] = []
        pm = self._pm()

        for provider in providers:
            try:
                # SDK-contract providers take the FingerprintRequest; legacy
                # providers (fnack.acoustid until its extraction PR) take the
                # file path — the service adapts per provider.
                if _is_sdk_fingerprinter(provider):
                    raw = pm.invoke_provider(
                        provider, "identify", request, timeout=_provider_timeout(provider, pm))
                else:
                    raw = pm.invoke_provider(
                        provider, "identify", request.file_path,
                        timeout=_provider_timeout(provider, pm))
            except BaseException as exc:  # noqa: BLE001 - untrusted provider
                manifest = getattr(provider, "manifest", None)
                pid = getattr(manifest, "id", "") if manifest else ""
                evidence.append(FingerprintEvidence(
                    provider_id=pid,
                    status="error",
                    error_code="provider_error",
                    retryable=True,
                    raw={"error": str(exc)[:500]},
                ))
                continue
            try:
                ev = self._normalize(provider, raw)
            except Exception:
                ev = None
            if ev is not None:
                evidence.append(ev)
        return evidence


def _provider_timeout(provider, pm) -> float:
    """Per-provider identify timeout (fingerprinting can be slow — fpcalc +
    lookup). The manager's default is fine; providers may expose their own."""
    from plugins.manager import DEFAULT_HOOK_TIMEOUT
    try:
        t = getattr(provider, "timeout", None)
        if t:
            return float(t)
    except Exception:
        pass
    return DEFAULT_HOOK_TIMEOUT


def _is_sdk_fingerprinter(provider) -> bool:
    """True when the provider implements the FINAL SDK FingerprintProvider
    contract (request-object based: identify(request)). Legacy providers
    (fnack.acoustid until its own extraction PR) take the file path —
    detected by signature, since runtime_checkable only checks member
    presence, not the call shape."""
    import inspect
    method = getattr(provider, "identify", None)
    if method is None:
        return False
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    params = [p for p in sig.parameters.values()
              if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    if not params:
        return False
    first = params[0]
    if first.name == "self" and len(params) > 1:
        first = params[1]
    # SDK contract: the argument is a FingerprintRequest object.
    if first.name == "request":
        return True
    if first.annotation is not inspect.Parameter.empty:
        ann = getattr(first.annotation, "__name__", str(first.annotation))
        if "FingerprintRequest" in ann:
            return True
    return False
