"""VPN management service for fnack.

Optional in-container VPN (OpenVPN or WireGuard). Configs live in the mounted
/config/vpn/ directory and are started automatically at boot (entrypoint) or
manually via the Settings UI. All container traffic routes through the tunnel.
"""

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("fnack.vpn")

VPN_DIR = Path(os.environ.get("VPN_DIR", "/config/vpn"))
LOG_FILE = "/tmp/fnack-vpn.log"


def _clear_spotiflac_breaker() -> None:
    """After the tunnel is up we have a fresh public IP — reset the SpotiFLAC
    429 circuit breaker so lossless downloads are retried immediately instead
    of waiting out the 30-300s cool-down on the old rate-limited IP."""
    try:
        from services.spotiflac_service import reset_spotiflac_rate_limit
        reset_spotiflac_rate_limit()
    except Exception as e:
        logger.debug("[VPN] Could not reset SpotiFLAC breaker: %s", e)


def _openvpn_configs() -> list:
    if not VPN_DIR.is_dir():
        return []
    return sorted(VPN_DIR.glob("*.ovpn"))


def _wireguard_configs() -> list:
    if not VPN_DIR.is_dir():
        return []
    return sorted(VPN_DIR.glob("*.conf"))


def is_configured() -> bool:
    return bool(_openvpn_configs() or _wireguard_configs())


def _vpn_processes() -> list:
    """Return running VPN processes (openvpn or wg-quick)."""
    out = []
    try:
        r = subprocess.run(["pgrep", "-af", "openvpn|wg-quick"], capture_output=True, text=True, timeout=5)
        for line in (r.stdout or "").splitlines():
            if "pgrep" in line:
                continue
            out.append(line.strip())
    except Exception:
        pass
    return out


def _tun_interfaces() -> list:
    try:
        r = subprocess.run(["ip", "-o", "link", "show"], capture_output=True, text=True, timeout=5)
        return [
            m.group(1)
            for m in re.finditer(r"^\d+:\s+(\S+):", (r.stdout or ""), re.M)
            if m.group(1).startswith(("tun", "wg"))
        ]
    except Exception:
        return []


def get_public_ip(timeout: float = 5.0) -> Optional[str]:
    """Current public IPv4 (through the VPN when the tunnel is up)."""
    for svc in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", str(int(timeout)), svc],
                capture_output=True, text=True, timeout=int(timeout) + 2,
            )
            ip = (r.stdout or "").strip()
            if ip and re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                return ip
        except Exception:
            continue
    return None


def get_vpn_status() -> dict:
    """Return current VPN state for the UI."""
    procs = _vpn_processes()
    tun = _tun_interfaces()
    cfg = _openvpn_configs() + _wireguard_configs()
    running = bool(procs) and bool(tun)
    public_ip = None
    if running:
        public_ip = get_public_ip()
    handshake_age = _wg_handshake_age() if _wireguard_configs() else None
    return {
        "configured": is_configured(),
        "running": running,
        "processes": procs[:3],
        "interfaces": tun,
        "config_files": [c.name for c in cfg],
        "type": "wireguard" if _wireguard_configs() and not _openvpn_configs() else "openvpn" if _openvpn_configs() else None,
        "public_ip": public_ip,
        "vpn_dir": str(VPN_DIR),
        "split_mode": _proxy_running(),
        "proxy": f"127.0.0.1:{_PROXY_PORT}" if _proxy_running() else None,
        "handshake_age_seconds": handshake_age,
        "handshake_ok": handshake_age is not None and handshake_age < 180,
    }


def _restore_docker_dns() -> None:
    """Keep the container's DNS working after wg-quick repoints /etc/resolv.conf.

    wg-quick writes the VPN's DNS servers into /etc/resolv.conf; when the peer
    handshake is incomplete those servers are unreachable and every name lookup
    fails ("Temporary failure in name resolution"). We restore the original
    resolver — the tunnel still routes traffic (fresh egress IP), only DNS
    queries go through the host resolver.
    """
    try:
        if not os.path.exists(_RESOLV_BACKUP):
            return
        with open(_RESOLV_BACKUP, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return
        with open("/etc/resolv.conf", "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("[VPN] Restored container DNS (host resolver) after tunnel up")
    except Exception as e:
        logger.warning("[VPN] Could not restore /etc/resolv.conf: %s", e)
    finally:
        try:
            os.remove(_RESOLV_BACKUP)
        except OSError:
            pass


_RESOLV_BACKUP = "/tmp/fnack-resolv-backup.conf"
_PROXY_PORT = int(os.environ.get("FNACK_VPN_PROXY_PORT", "1080"))
_PROXY_UID = 2001
_PROXY_SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "http_proxy.py")
_PROXY_ENV = {
    "HTTP_PROXY": f"http://127.0.0.1:{_PROXY_PORT}",
    "HTTPS_PROXY": f"http://127.0.0.1:{_PROXY_PORT}",
    "ALL_PROXY": f"http://127.0.0.1:{_PROXY_PORT}",
    "NO_PROXY": "localhost,127.0.0.1,::1,192.168.0.0/16,172.16.0.0/12,100.64.0.0/10",
}


def _run_ip(*args) -> None:
    subprocess.run(["ip", *args], capture_output=True, text=True, timeout=10)


def _enable_split_mode() -> None:
    """Keep the container's normal routing; route only the proxy's sockets
    (uid 2001) via the WireGuard tunnel, then point downloads/metadata at the
    proxy. The web dashboard and LAN traffic stay direct."""
    _run_ip("rule", "del", "not", "fwmark", "51820", "table", "51820")
    _run_ip("rule", "del", "table", "main", "suppress_prefixlength", "0")
    _run_ip("rule", "add", f"uidrange", f"{_PROXY_UID}-{_PROXY_UID}", "lookup", "51820")
    if not _proxy_running():
        try:
            subprocess.Popen(
                ["python3", _PROXY_SCRIPT, "--port", str(_PROXY_PORT), "--uid", str(_PROXY_UID)],
                stdout=open("/tmp/fnack-http-proxy.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            logger.warning("[VPN] Could not start HTTP proxy: %s", e)
    for k, v in _PROXY_ENV.items():
        os.environ[k] = v
    logger.info("[VPN] Split mode active: downloads/metadata via 127.0.0.1:%d, dashboard direct", _PROXY_PORT)


def _disable_split_mode() -> None:
    _run_ip("rule", "del", "uidrange", f"{_PROXY_UID}-{_PROXY_UID}", "lookup", "51820")
    subprocess.run(["pkill", "-f", "scripts/http_proxy.py"], capture_output=True, timeout=10)
    for k in _PROXY_ENV:
        os.environ.pop(k, None)


def _proxy_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "scripts/http_proxy.py"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _wg_handshake_age() -> Optional[float]:
    """Seconds since the last successful WireGuard handshake (None = never)."""
    try:
        r = subprocess.run(["wg", "show", "wg0", "latest-handshakes"], capture_output=True, text=True, timeout=5)
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "wg0":
                ts = int(parts[1])
                if ts == 0:
                    return None
                return max(0.0, time.time() - ts)
    except Exception:
        pass
    return None


def start_vpn() -> Tuple[bool, str]:
    """Start the configured VPN (OpenVPN or WireGuard) in the background."""
    if _vpn_processes():
        return True, "VPN already running"

    if not is_configured():
        return False, "No VPN config found. Upload an OpenVPN (.ovpn) or WireGuard (.conf) file first."

    try:
        VPN_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"Cannot create {VPN_DIR}: {e}"

    # Back up the working Docker resolver before wg-quick can hijack it
    try:
        import shutil
        shutil.copy2("/etc/resolv.conf", _RESOLV_BACKUP)
    except Exception:
        pass

    # OpenVPN takes precedence when present
    ovpn = _openvpn_configs()
    if ovpn:
        cfg = ovpn[0]
        log = open(LOG_FILE, "w")
        try:
            proc = subprocess.Popen(
                ["openvpn", "--config", str(cfg), "--cd", str(VPN_DIR)],
                stdout=log, stderr=subprocess.STDOUT,
            )
        except Exception as e:
            log.close()
            return False, f"Failed to launch OpenVPN: {e}"
        time.sleep(4)
        if not _tun_interfaces():
            return False, "OpenVPN started but the tunnel did not come up. Check the VPN logs (Settings -> VPN or docker logs)."
        logger.info("[VPN] OpenVPN tunnel up via %s", cfg.name)
        _clear_spotiflac_breaker()
        return True, f"OpenVPN connected via {cfg.name}"

    # WireGuard
    wg = _wireguard_configs()[0]
    try:
        r = subprocess.run(["wg-quick", "up", str(wg)], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            logger.info("[VPN] WireGuard tunnel up via %s", wg.name)
            _enable_split_mode()
            _restore_docker_dns()
            _clear_spotiflac_breaker()
            return True, f"WireGuard connected via {wg.name} (split mode: downloads via tunnel, dashboard direct)"
        err_text = (r.stderr or r.stdout or "").strip()[:300]
        logger.warning("[VPN] WireGuard failed to start via %s: %s", wg.name, err_text)
        _restore_docker_dns()
        return False, f"WireGuard failed to start: {err_text}"
    except Exception as e:
        logger.warning("[VPN] WireGuard error via %s: %s", wg.name, e)
        _restore_docker_dns()
        return False, f"WireGuard error: {e}"


def stop_vpn() -> Tuple[bool, str]:
    """Stop the running VPN tunnel."""
    _disable_split_mode()
    stopped = False
    try:
        subprocess.run(["pkill", "-f", "openvpn --config"], capture_output=True, timeout=10)
        stopped = True
    except Exception:
        pass
    for wg in _wireguard_configs():
        try:
            subprocess.run(["wg-quick", "down", str(wg)], capture_output=True, text=True, timeout=30)
            stopped = True
        except Exception:
            pass
    return stopped, "VPN stopped" if stopped else "No VPN was running"


def save_vpn_config(content: str, filename: str) -> Tuple[bool, str]:
    """Save an uploaded VPN config to /config/vpn/ (safe filename)."""
    name = os.path.basename((filename or "").strip())
    if not name or not re.match(r"^[A-Za-z0-9._-]+$", name):
        return False, "Invalid config filename"
    if not (name.endswith(".ovpn") or name.endswith(".conf")):
        return False, "Config must be .ovpn (OpenVPN) or .conf (WireGuard)"
    if not content or len(content) < 20:
        return False, "Config content is too short"

    # Normalize Windows line endings — wg-quick chokes on stray \r characters
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    # Refuse obvious secrets-harvesting content (comment-only / plain URL)
    lowered = content.lower()
    if "remote " not in lowered and "interface" not in lowered and "dev tun" not in lowered:
        return False, "This does not look like a VPN config (missing 'remote' / 'interface' directives)"

    # WireGuard configs need [Interface] with a PrivateKey and at least one [Peer]
    if name.endswith(".conf"):
        if "[interface]" not in lowered or "privatekey" not in lowered:
            return False, (
                "This does not look like a valid WireGuard config: it must contain a "
                "[Interface] section with a PrivateKey (and at least one [Peer])."
            )
        if "[peer]" not in lowered:
            return False, (
                "This WireGuard config has no [Peer] section. Add the server's "
                "PublicKey and Endpoint (and your AllowedIPs) under [Peer]."
            )

    try:
        VPN_DIR.mkdir(parents=True, exist_ok=True)
        dest = VPN_DIR / name
        dest.write_text(content, encoding="utf-8")
        # Private keys in the config must not be world-readable (wg-quick warns
        # "config is world accessible" and some setups refuse to start).
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        logger.info("[VPN] Saved VPN config %s", dest)
        return True, f"VPN config saved as {name}. Restart the VPN to apply it."
    except OSError as e:
        return False, f"Failed to save config: {e}"


def delete_vpn_config() -> Tuple[bool, str]:
    """Remove all VPN configs (and stop the VPN)."""
    stop_vpn()
    removed = []
    for c in list(_openvpn_configs()) + list(_wireguard_configs()):
        try:
            c.unlink()
            removed.append(c.name)
        except OSError:
            pass
    return bool(removed), f"Removed VPN config(s): {', '.join(removed)}" if removed else "No VPN configs to remove"
