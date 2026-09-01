"""MediaServerService — application service for the media capabilities.

Phase 3 (brief 03): scan / health / test_connection resolve
media.scan / media.health / media.connection_test through the capability
registry (enabled, priority-ordered providers) and invoke the best provider
through the manager's ProviderExecutor boundary. The service must NOT know
Navidrome (or any provider) — it operates on capabilities.

Candidate configuration (brief §Candidate configuration): connection tests
support UNSAVED settings — `test_connection(candidate_config)` passes the
user's typed-but-not-saved values to providers that accept them (signature
inspection), so the settings UI can validate a candidate config before
saving. This removes the justification for direct core provider-service
access.

Zero enabled providers -> CapabilityUnavailable(capability, operation) — a
valid state (MASTER rule 3); no hidden provider fallback.
"""

from __future__ import annotations

from typing import Any, Optional

from fnack.plugin_api.capabilities import (
    MEDIA_CONNECTION_TEST,
    MEDIA_HEALTH,
    MEDIA_SCAN,
)
from fnack.plugin_api.errors import CapabilityUnavailable


class MediaServerService:
    """Owns media capability resolution + first-success policy."""

    def __init__(self, manager=None):
        self._manager = manager  # injectable for tests

    # -- resolution ---------------------------------------------------------

    def _pm(self):
        if self._manager is not None:
            return self._manager
        from plugins.manager import plugin_manager
        return plugin_manager

    def _providers_for(self, capability: str, operation: str) -> list:
        pm = self._pm()
        if pm is None:
            raise CapabilityUnavailable(capability, operation, "plugin manager not ready")
        if not pm.has_capability(capability):
            raise CapabilityUnavailable(capability, operation)
        return pm.get_capability_providers(capability)

    def _invoke(self, provider, method_name, *args, **kwargs):
        pm = self._pm()
        if pm is None:
            return None
        return pm.invoke_provider(provider, method_name, *args, **kwargs)

    # -- media.scan ---------------------------------------------------------

    def scan(self) -> tuple[bool, str]:
        """Trigger a media-server scan (first provider returning a usable
        result wins). CapabilityUnavailable when no media.scan provider is
        enabled."""
        providers = self._providers_for(MEDIA_SCAN, "scan")
        for provider in providers:
            try:
                ok, msg = self._invoke(provider, "trigger_scan")
            except Exception:
                continue
            if ok:
                return True, msg or "scan triggered"
        return False, "no media server returned a successful scan"

    # -- media.health -------------------------------------------------------

    def health(self) -> Optional[dict]:
        """Media-server health (first provider returning a non-empty dict
        wins). CapabilityUnavailable when no media.health provider is
        enabled."""
        providers = self._providers_for(MEDIA_HEALTH, "health")
        for provider in providers:
            try:
                h = self._invoke(provider, "health")
            except Exception:
                continue
            if h:
                return h
        return None

    # -- media.connection_test ----------------------------------------------

    def test_connection(self, candidate_config: Optional[dict] = None) -> tuple[bool, str]:
        """Test the connection, optionally with UNSAVED candidate settings
        (brief §Candidate configuration). The candidate config is forwarded
        to providers whose `test_connection` accepts a config argument
        (signature inspection); providers that only read their stored config
        are called without it. CapabilityUnavailable when no
        media.connection_test provider is enabled."""
        providers = self._providers_for(MEDIA_CONNECTION_TEST, "test_connection")
        for provider in providers:
            try:
                if candidate_config and "candidate_config" in _accepts_keywords(provider, "test_connection"):
                    ok, msg = self._invoke(provider, "test_connection", candidate_config=candidate_config)
                else:
                    ok, msg = self._invoke(provider, "test_connection")
            except Exception:
                continue
            if ok:
                return True, msg or "connection OK"
        return False, "no media server accepted the connection test"


def _accepts_keywords(provider, method_name: str) -> set:
    """Keyword args the provider's method accepts (signature inspection)."""
    import inspect
    method = getattr(provider, method_name, None)
    if method is None:
        return set()
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return set()
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return {"**kwargs"}
    return {p for p, pv in params.items()
            if pv.kind in (inspect.Parameter.KEYWORD_ONLY,
                           inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and p != "self"}
