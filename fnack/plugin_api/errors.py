"""SDK error types.

The stable error contract between core, plugins, and application services:

- `PluginError`       — base class for everything plugin-related.
- `CapabilityUnavailable` — raised when no *enabled* plugin supplies a
  requested capability. Missing capability is a *valid* state (MASTER rule
  3): core must return this structured error, never fall back to a legacy
  provider service.
- `ProviderError`     — a specific provider failed while serving a
  capability (with a stable machine-readable `code` and `retryable` flag so
  application services can decide whether to try the next provider).
"""

from __future__ import annotations

from typing import Optional


class PluginError(Exception):
    """Base class for all fnack plugin-SDK errors."""


class CapabilityUnavailable(PluginError):
    def __init__(self, capability: str, operation: str, message: Optional[str] = None):
        self.capability = capability
        self.operation = operation
        super().__init__(
            message or f"No enabled plugin provides capability '{capability}'"
        )


class ProviderError(PluginError):
    def __init__(
        self,
        provider_id: str,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ):
        self.provider_id = provider_id
        self.code = code
        self.retryable = retryable
        super().__init__(message)
