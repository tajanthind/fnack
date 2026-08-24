"""Navidrome / Subsonic integration service for fnack.

Allows fnack to automatically trigger library scans on Navidrome when new music
is downloaded, imported, or modified, or when triggered manually by the user.
"""

import hashlib
import logging
import secrets
import threading
import time
from typing import Optional, Tuple
import requests

logger = logging.getLogger("fnack.navidrome")
TIMEOUT = 8

# Client-side scan debounce: with max_concurrent=1 downloads finish minutes
# apart, but with faster settings several tracks can complete within seconds.
# Navidrome re-scans the whole library, so firing once per 30s window is enough.
_SCAN_DEBOUNCE_SECONDS = 30.0
_scan_lock = threading.Lock()
_last_scan_time = 0.0


def _get_auth_params(user: str, token_or_pass: str) -> dict:
    """Build standard Subsonic API auth params supporting both plain token and MD5 token+salt."""
    if not user:
        return {}
    
    salt = secrets.token_hex(6)
    token = hashlib.md5(f"{token_or_pass}{salt}".encode("utf-8")).hexdigest()
    
    return {
        "u": user,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "fnack",
        "f": "json",
    }


def test_navidrome_connection(url: str, user: str = "", token_or_pass: str = "") -> Tuple[bool, str]:
    """Test connection to Navidrome / Subsonic server using ping.view."""
    clean_url = (url or "").strip().rstrip("/")
    if not clean_url:
        return False, "Navidrome URL is required"

    if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        clean_url = f"http://{clean_url}"

    ping_url = f"{clean_url}/rest/ping.view"
    params = _get_auth_params(user, token_or_pass)

    try:
        resp = requests.get(ping_url, params=params, timeout=TIMEOUT)
        if resp.status_code == 200:
            try:
                data = resp.json()
                sub_resp = data.get("subsonic-response", {})
                status = sub_resp.get("status")
                if status == "ok":
                    server_ver = sub_resp.get("serverVersion", "Unknown")
                    return True, f"Connected to Navidrome (Subsonic v{server_ver})"
                elif "error" in sub_resp:
                    err = sub_resp["error"]
                    return False, f"Navidrome error: {err.get('message', 'Authentication failed')} (code {err.get('code')})"
            except Exception:
                return True, "Navidrome server responded (HTTP 200)"
        elif resp.status_code == 401:
            return False, "Navidrome authentication failed (401 Unauthorized)"
        else:
            return False, f"Navidrome returned HTTP status {resp.status_code}"
    except requests.exceptions.RequestException as e:
        logger.warning("[NAVIDROME] Connection test failed for %s: %s", clean_url, e)
        return False, f"Cannot reach Navidrome at {clean_url}: {e}"

    return False, "Unknown response from Navidrome"


def trigger_navidrome_scan(app) -> Tuple[bool, str]:
    """Trigger library rescan on configured Navidrome server (debounced)."""
    from models import AppSetting, db

    global _last_scan_time
    with _scan_lock:
        now = time.time()
        if now - _last_scan_time < _SCAN_DEBOUNCE_SECONDS:
            return False, "Scan already triggered recently (debounced)"
        _last_scan_time = now

    with app.app_context():
        s_url = db.session.get(AppSetting, "navidrome_url")
        s_user = db.session.get(AppSetting, "navidrome_user")
        s_token = db.session.get(AppSetting, "navidrome_token")
        s_auto = db.session.get(AppSetting, "navidrome_auto_scan")

        url = s_url.value.strip() if s_url else ""
        user = s_user.value.strip() if s_user else ""
        token = s_token.value.strip() if s_token else ""
        auto = s_auto.value.lower() != "false" if s_auto else True

    if not url or not auto:
        return False, "Navidrome scan not configured or disabled"

    clean_url = url.rstrip("/")
    if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        clean_url = f"http://{clean_url}"

    scan_url = f"{clean_url}/rest/startScan.view"
    params = _get_auth_params(user, token)

    try:
        logger.info("[NAVIDROME] Triggering library scan at %s", clean_url)
        resp = requests.get(scan_url, params=params, timeout=TIMEOUT)
        if resp.status_code == 200:
            logger.info("[NAVIDROME] Successfully triggered Navidrome library scan")
            return True, "Navidrome library scan initiated"
        else:
            logger.warning("[NAVIDROME] Scan request failed with status %d: %s", resp.status_code, resp.text)
            return False, f"Navidrome returned status {resp.status_code}"
    except Exception as e:
        logger.warning("[NAVIDROME] Failed to trigger scan: %s", e)
        return False, f"Failed to reach Navidrome: {e}"
