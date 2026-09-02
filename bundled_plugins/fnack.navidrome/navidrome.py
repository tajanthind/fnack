"""Navidrome / Subsonic integration service for fnack.

Allows fnack to automatically trigger library scans on Navidrome when new music
is downloaded, imported, or modified, or when triggered manually by the user.

Also repairs Navidrome album splits: Navidrome 0.63 identifies albums by
(album artist, album name, release date), and files that carried per-track
originaldate/releasedate tags used to make it split one album into many rows.
fnack v0.2.24+ strips those tags, and consolidate_split_albums() merges the
already-split rows. run_auto_split_repair() is called automatically at every
fnack boot and periodically (when navidrome_db_path is configured).
"""

import hashlib
import logging
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
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


def consolidate_split_albums(db_path) -> dict:
    """Merge split album rows in a Navidrome SQLite database.

    For every (album artist, album name) group with more than one row, keeps
    the row with the most songs, repoints all media files to it, merges cover
    art links, deletes the leftover rows and rebuilds the album FTS. A
    consistent snapshot backup (navidrome.db.bak-<timestamp>) is written
    before anything is modified. Favorites/playlists survive because media
    file IDs are unchanged.

    Safe to run repeatedly (idempotent) and safe to run while Navidrome is
    stopped; when Navidrome is running, follow it with a rescan so it reloads.
    Returns {"groups", "merged_rows", "moved_files"}.
    """
    import sqlite3
    import shutil

    db_path = str(db_path)
    stats = {"groups": 0, "merged_rows": 0, "moved_files": 0}

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA busy_timeout=30000")

    # ---- schema guards: refuse to touch an unexpected schema ----
    album_cols = {r[1] for r in cur.execute("PRAGMA table_info(album)")}
    file_cols = {r[1] for r in cur.execute("PRAGMA table_info(media_file)")}
    for need in ("id", "album_artist", "name", "song_count", "embed_art_path",
                 "small_image_url", "large_image_url", "release_date"):
        if need not in album_cols:
            logger.warning("[NAVIDROME] Split-repair aborted: unexpected album schema (no '%s')", need)
            conn.close()
            return stats
    for need in ("album_id", "missing"):
        if need not in file_cols:
            logger.warning("[NAVIDROME] Split-repair aborted: unexpected media_file schema (no '%s')", need)
            conn.close()
            return stats

    groups = cur.execute(
        "SELECT album_artist, name, COUNT(*) c FROM album "
        "GROUP BY album_artist, name HAVING COUNT(*) > 1"
    ).fetchall()
    if not groups:
        conn.close()
        return stats
    stats["groups"] = len(groups)

    # Consistent snapshot before modifying anything
    try:
        bak = f"{db_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(bak)
        src.backup(dst)
        dst.close()
        src.close()
        logger.info("[NAVIDROME] Backed up Navidrome DB to %s", bak)
    except Exception as e:
        logger.warning("[NAVIDROME] Could not back up Navidrome DB: %s", e)

    for g in groups:
        rows = cur.execute(
            "SELECT id, embed_art_path, small_image_url, large_image_url, release_date, date "
            "FROM album WHERE album_artist=? AND name=? ORDER BY song_count DESC",
            (g["album_artist"], g["name"]),
        ).fetchall()
        if len(rows) < 2:
            continue
        canon = rows[0]
        others = [r for r in rows[1:]]
        other_ids = [r["id"] for r in others]
        placeholders = ",".join("?" * len(other_ids))

        cur.execute(
            f"UPDATE media_file SET album_id=? WHERE album_id IN ({placeholders})",
            [canon["id"]] + other_ids,
        )
        stats["moved_files"] += cur.rowcount

        for o in others:
            if not canon["embed_art_path"] and o["embed_art_path"]:
                cur.execute("UPDATE album SET embed_art_path=? WHERE id=?", (o["embed_art_path"], canon["id"]))
            for col in ("small_image_url", "large_image_url"):
                if not canon[col] and o[col]:
                    cur.execute(f"UPDATE album SET {col}=? WHERE id=?", (o[col], canon["id"]))
            if not canon["release_date"] and o["release_date"]:
                cur.execute("UPDATE album SET release_date=? WHERE id=?", (o["release_date"], canon["id"]))
            if not canon["date"] and o["date"]:
                cur.execute("UPDATE album SET date=? WHERE id=?", (o["date"], canon["id"]))

        cur.execute(f"DELETE FROM album WHERE id IN ({placeholders})", other_ids)
        stats["merged_rows"] += len(others)

        n = cur.execute(
            "SELECT COUNT(*) c FROM media_file WHERE album_id=? AND missing=0", (canon["id"],)
        ).fetchone()["c"]
        cur.execute("UPDATE album SET song_count=? WHERE id=?", (n, canon["id"]))

    # Rebuild the album full-text index so search stays consistent
    try:
        cur.execute("INSERT INTO album_fts(album_fts) VALUES('rebuild')")
    except Exception:
        pass  # some schemas rebuild FTS on the next scan

    conn.commit()
    conn.close()

    logger.info(
        "[NAVIDROME] Split-repair: %d group(s), %d album row(s) merged, %d media file(s) repointed",
        stats["groups"], stats["merged_rows"], stats["moved_files"],
    )
    return stats


def run_auto_split_repair(config: Optional[dict] = None, app=None) -> dict:
    """Auto-fix Navidrome album splits at boot / periodically.

    `config` is plugin-owned (Phase 4): {"db_path"}. When set, consolidates
    split album rows (with a snapshot backup) and triggers a Navidrome
    rescan if anything was merged (using the plugin's scan config). Returns
    a status dict.
    """
    config = config or {}
    db_path = (config.get("db_path") or "").strip()
    if not db_path:
        db_path = os.environ.get("NAVIDROME_DB_PATH", "").strip()
    if not db_path:
        return {"enabled": False, "reason": "no navidrome_db_path configured"}
    if not Path(db_path).is_file():
        logger.warning("[NAVIDROME] Split-repair: configured DB not found at %s", db_path)
        return {"enabled": True, "error": f"DB not found: {db_path}"}

    try:
        stats = consolidate_split_albums(db_path)
        if stats["merged_rows"] > 0:
            trigger_navidrome_scan(config)
        return {"enabled": True, **stats}
    except Exception as e:
        logger.exception("[NAVIDROME] Split-repair failed: %s", e)
        return {"enabled": True, "error": str(e)}


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


def trigger_navidrome_scan(config: Optional[dict] = None) -> Tuple[bool, str]:
    """Trigger library rescan on the configured Navidrome server (debounced).

    `config` is plugin-owned (Phase 4: provider settings live in the plugin):
    {"url", "user", "token", "auto_scan"}. The plugin injects it; no core
    AppSetting reads.
    """
    config = config or {}

    global _last_scan_time
    with _scan_lock:
        now = time.time()
        if now - _last_scan_time < _SCAN_DEBOUNCE_SECONDS:
            return False, "Scan already triggered recently (debounced)"
        _last_scan_time = now

    url = (config.get("url") or "").strip()
    user = (config.get("user") or "").strip()
    token = (config.get("token") or "").strip()
    auto = str(config.get("auto_scan", "true")).lower() != "false"

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
