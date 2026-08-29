"""Optional AcoustID fingerprinting for fnack.

Zero-auth by design: fingerprinting is strictly optional. Without an
`acoustid_api_key` setting everything else works unchanged (no fingerprint
lookups, no "Identify this file" action). With a free acoustid.org client
key configured, fnack can:

  * verify a downloaded file when the tag/duration verifier is unsure or
    about to delete it — a strong match (>= 0.8 + cross-checks) confirms
    "right file, wrong tags" and lets the finalize step retag it; a strong
    mismatch flags the track with a caution mark instead of silently
    deleting it;
  * identify an unknown file (empty/wrong tags) via the manual "Identify
    this file" flow.

Regional artists with no fingerprint match are a silent no-op: the existing
verifier verdict stands, no error, no retry, no UI blocking.

Depends on `fpcalc` (apt `libchromaprint-tools`) — installed in the image.
"""

import json
import logging
import os
import re
import subprocess
from typing import Optional

import requests

from models import AppSetting, db

logger = logging.getLogger("fnack.acoustid")

API_URL = "https://api.acoustid.org/v2/lookup"
CONFIRM_THRESHOLD = 0.8
DISPLAY_THRESHOLD = 0.4
# AcoustID guideline: <= 3 requests/second. We pace generously to 1.2s.
_MIN_INTERVAL = 1.2
_last_request = 0.0


def _get_key() -> str:
    s = db.session.get(AppSetting, "acoustid_api_key")
    return (s.value or "").strip() if s else ""


def is_enabled() -> bool:
    return bool(_get_key())


def _pace() -> None:
    global _last_request
    import time
    wait = _MIN_INTERVAL - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()


def _parse_fpcalc(stdout: str) -> Optional[tuple]:
    """Extract (duration_seconds, fingerprint) from fpcalc output."""
    duration = None
    fingerprint = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("DURATION="):
            try:
                duration = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("FINGERPRINT="):
            fingerprint = line.split("=", 1)[1].strip()
    if duration is not None and fingerprint:
        return duration, fingerprint
    return None


def fingerprint_file(path: str) -> Optional[tuple]:
    """Run fpcalc on a file. Returns (duration_seconds, fingerprint) or None."""
    try:
        r = subprocess.run(
            ["fpcalc", "-length", "120", "-raw", path],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            logger.debug("[ACOUSTID] fpcalc failed: %s", (r.stderr or "").strip()[:200])
            return None
        return _parse_fpcalc(r.stdout)
    except Exception as e:
        logger.debug("[ACOUSTID] fpcalc error: %s", e)
        return None


def lookup(fingerprint: str, duration: Optional[float], limit: int = 5) -> list:
    """Query AcoustID by fingerprint. Returns a list of result dicts with
    score + recordings (title, artists, isrcs, releasegroups)."""
    key = _get_key()
    if not key:
        return []
    _pace()
    params = {
        "client": key,
        "fingerprint": fingerprint,
        "meta": "recordings+releasegroups+isrcs",
        "format": "json",
    }
    if duration:
        params["duration"] = int(round(duration))
    try:
        resp = requests.get(API_URL, params=params, timeout=15)
        if resp.status_code != 200:
            logger.warning("[ACOUSTID] lookup HTTP %s", resp.status_code)
            return []
        data = resp.json()
    except Exception as e:
        logger.warning("[ACOUSTID] lookup error: %s", e)
        return []

    out = []
    for res in (data.get("results") or [])[:limit]:
        recs = []
        for rec in (res.get("recordings") or [])[:6]:
            artists = [a.get("name", "") for a in (rec.get("artists") or []) if a.get("name")]
            recs.append({
                "id": rec.get("id", ""),
                "title": rec.get("title", ""),
                "artists": artists,
                "isrcs": rec.get("isrcs") or [],
                "duration": rec.get("duration"),
                "releasegroups": [rg.get("title", "") for rg in (rec.get("releasegroups") or [])[:3]],
            })
        if recs:
            out.append({"score": float(res.get("score") or 0.0), "recordings": recs})
    return out


def _norm(s) -> str:
    if not s:
        return ""
    return re.sub(r"[^a-zA-Z0-9]+", "", s).lower()


def _candidate_matches(cand: dict, artist: Optional[str], title: Optional[str], duration: Optional[float]) -> bool:
    """Cross-check a recording candidate against the expected track."""
    title_ok = not title or _norm(cand.get("title")) == _norm(title) or _norm(title) in _norm(cand.get("title")) or _norm(cand.get("title")) in _norm(title)
    artist_ok = not artist or any(_norm(a) == _norm(artist) for a in cand.get("artists") or [])
    duration_ok = True
    if duration and cand.get("duration"):
        duration_ok = abs(float(cand["duration"]) - float(duration)) <= max(10.0, float(duration) * 0.15)
    return title_ok and artist_ok and duration_ok


def verify_download(path: str, expected_artist: Optional[str], expected_title: Optional[str],
                    expected_duration: Optional[float]) -> dict:
    """Fingerprint a downloaded file and decide:
      * {"status": "match",    "info": ...}  — same song, wrong tags (accept + retag)
      * {"status": "mismatch", "info": ...}  — a DIFFERENT song (flag, don't delete)
      * {"status": "unknown",  "info": ...}  — no fingerprint / no match (verifier stands)
    """
    if not is_enabled():
        return {"status": "unknown", "info": None}
    fp = fingerprint_file(path)
    if not fp:
        return {"status": "unknown", "info": None}
    duration, fingerprint = fp
    results = lookup(fingerprint, duration)
    if not results:
        return {"status": "unknown", "info": None}

    # Best candidate overall (highest score)
    best = max(results, key=lambda r: r["score"])
    best_rec = best["recordings"][0] if best["recordings"] else None
    info = {
        "score": round(best["score"], 3),
        "matched_title": best_rec.get("title") if best_rec else None,
        "matched_artists": (best_rec.get("artists") or []) if best_rec else [],
        "matched_isrcs": (best_rec.get("isrcs") or []) if best_rec else [],
    }

    if best["score"] >= CONFIRM_THRESHOLD and best_rec:
        if _candidate_matches(best_rec, expected_artist, expected_title, expected_duration):
            return {"status": "match", "info": info}
        # High-confidence result that is NOT the expected track
        return {"status": "mismatch", "info": info}

    # Lower confidence: only accept when the candidate still matches the tags
    for res in results:
        if res["score"] >= 0.5:
            for rec in res["recordings"]:
                if _candidate_matches(rec, expected_artist, expected_title, expected_duration):
                    return {"status": "match", "info": {
                        "score": round(res["score"], 3),
                        "matched_title": rec.get("title"),
                        "matched_artists": rec.get("artists") or [],
                        "matched_isrcs": rec.get("isrcs") or [],
                    }}
    return {"status": "unknown", "info": info}


def identify(path: str) -> list:
    """Fingerprint + lookup for the manual 'Identify this file' flow.
    Returns candidates >= DISPLAY_THRESHOLD for the user to pick from."""
    if not is_enabled():
        return []
    fp = fingerprint_file(path)
    if not fp:
        return []
    duration, fingerprint = fp
    results = lookup(fingerprint, duration, limit=10)
    out = []
    for res in results:
        if res["score"] < DISPLAY_THRESHOLD:
            continue
        for rec in res["recordings"]:
            out.append({
                "score": round(res["score"], 3),
                "title": rec.get("title", ""),
                "artists": rec.get("artists") or [],
                "isrcs": rec.get("isrcs") or [],
                "duration": rec.get("duration"),
                "releasegroups": rec.get("releasegroups") or [],
            })
    # de-duplicate by (title, first artist)
    seen = set()
    dedup = []
    for c in out:
        key = (_norm(c["title"]), _norm((c["artists"] or [""])[0]))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(c)
    return dedup[:5]
