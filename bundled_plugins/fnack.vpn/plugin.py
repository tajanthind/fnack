"""Bundled first-party plugin: VPN (WireGuard/OpenVPN).

Wraps services/vpn_service.py 1:1. The split-mode HTTP CONNECT proxy
(scripts/http_proxy.py) is part of this plugin's machinery — it is spawned
by vpn_service, not a separate library_task (audit finding, Phase 1).
"""

from plugins.base import VPNPlugin
from services.vpn_service import get_vpn_status, start_vpn, stop_vpn


class VPNPluginImpl(VPNPlugin):
    def start(self) -> tuple[bool, str]:
        return start_vpn()

    def stop(self) -> tuple[bool, str]:
        return stop_vpn()

    def status(self) -> dict:
        return get_vpn_status()
