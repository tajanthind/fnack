"""Bundled first-party plugin: VPN (WireGuard/OpenVPN).

Wraps services/vpn_service.py 1:1. The split-mode HTTP CONNECT proxy
(scripts/http_proxy.py) is part of this plugin's machinery — it is spawned
by vpn_service, not a separate library_task (audit finding, Phase 1).

Brief 6 §2: settings surface is fully schema-driven — the `config_file`
"file"-type field uploads a config (stored as this plugin's private copy
under <config>/plugins/fnack.vpn/data/), `on_settings_changed` writes it
to VPN_DIR so vpn_service reads it, and the manifest `actions` array
declares Start/Stop buttons (POST /api/plugins/<id>/action/<id> calls
start()/stop()). Live status is exposed via status() and polled by the
settings modal.
"""

import shutil
from pathlib import Path

from plugins.base import VPNPlugin
from services.vpn_service import get_vpn_status, start_vpn, stop_vpn

VPN_DIR = Path(__import__("os").environ.get("VPN_DIR", "/config/vpn"))


class VPNPluginImpl(VPNPlugin):
    def start(self) -> tuple[bool, str]:
        return start_vpn()

    def stop(self) -> tuple[bool, str]:
        return stop_vpn()

    def status(self) -> dict:
        return get_vpn_status()

    def on_settings_changed(self, settings: dict) -> None:
        """Copy an uploaded config_file into VPN_DIR so vpn_service picks it
        up (it scans VPN_DIR for *.ovpn / *.conf)."""
        path = (settings or {}).get("config_file")
        if not path:
            return
        src = Path(str(path))
        if not src.is_file():
            return
        suffix = src.suffix.lower()
        if suffix not in (".ovpn", ".conf", ".wg"):
            return
        try:
            VPN_DIR.mkdir(parents=True, exist_ok=True)
            dest = VPN_DIR / src.name
            shutil.copy2(str(src), str(dest))
        except OSError as e:
            self.context.log.warning("Could not copy VPN config into %s: %s", VPN_DIR, e)
