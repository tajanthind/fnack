"""Bundled first-party plugin: reverse-proxy-header auth provider (optional).

Strictly opt-in: fnack's core is unauthenticated (zero required auth). This
plugin only takes effect when the user ENABLES it in Settings → Plugins; the
core before_request guard in app.py activates only when an auth_provider
plugin is enabled. Reads the trusted header (e.g. X-Forwarded-User set by
Authelia/Authentik) and returns the username.

Security note: the header is spoofable unless a trusted reverse proxy strips
incoming copies — the `trusted_proxy` setting is advisory; the real
protection is the deployment's proxy config.
"""

from typing import Optional

from plugins.base import AuthProviderPlugin


class ReverseProxyAuth(AuthProviderPlugin):
    def authenticate(self, request_headers: dict) -> Optional[str]:
        header = (self.context.settings.get("header_name") or "X-Forwarded-User").strip()
        user = (request_headers.get(header) or request_headers.get(header.lower()) or "").strip()
        return user or None
