"""MusicBrainz catalogue enrichment for fnack.

Enrichment-only by design:
  * Deezer remains the authoritative discography source.
  * MusicBrainz only ADDS release-group id / canonical title / first-release
    year to albums on confident matches — it never removes, renames or
    reorders Deezer data.
  * Regional artists not in MusicBrainz get a negative artist cache entry and
    are never probed per-album.
  * Stale MusicBrainz data can only add metadata, never delete correct data.
  * Every failure is fail-soft: discography sync never blocks on MusicBrainz.

Rate limiting: 1 request/second etiquette with a User-Agent containing
contact info; caches found (7d) / not-found (30d) / error (1h) results in
plugin-owned in-memory module state (Phase 4: provider cache lives in the
plugin, not in core DB models).
"""

import json
import logging
import re
import time
import unicodedata
from typing import Optional

import requests


logger = logging.getLogger("fnack.musicbrainz")

API = "https://musicbrainz.org/ws/2"
USER_AGENT = "fnack/0.2 (https://github.com/tajanthind/fnack)"
MIN_INTERVAL = 1.0
_last_request = 0.0
_TTL = {"found": 7 * 24 * 3600, "notfound": 30 * 24 * 3600, "error": 3600}

# Plugin-owned provider cache (Phase 4: provider state/cache lives in the
# plugin, not in core DB models). In-memory with the same TTLs (7d found /
# 30d negative / 1h error) + the 1 req/s pacing.
_cache: dict[str, tuple[float, bool, object]] = {}  # key -> (ts, found, payload)


def _norm(s) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    return re.sub(r"[^a-zA-Z0-9]+", "", s).lower()


def _pace() -> None:
    global _last_request
    wait = MIN_INTERVAL - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()


def _mb_get(path: str, params: dict, _retry: bool = True) -> Optional[dict]:
    _pace()
    try:
        resp = requests.get(
            f"{API}{path}", params=params, timeout=15,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
    except Exception as e:
        logger.debug("[MB] request error: %s", e)
        return None
    if resp.status_code == 200:
        try:
            return resp.json()
        except Exception:
            return None
    if resp.status_code == 503:  # rate-limited; back off with Retry-After, retry once
        try:
            backoff = min(30, max(2, int(resp.headers.get("Retry-After", "5"))))
        except Exception:
            backoff = 5
        logger.info("[MB] 503 rate limit; backing off %ss", backoff)
        time.sleep(backoff)
        if _retry:
            return _mb_get(path, params, _retry=False)
        return None
    logger.debug("[MB] HTTP %s for %s", resp.status_code, path)
    return None


def _cache_get(query: str, kind: str, ttl_seconds: float):
    import time as _time
    entry = _cache.get(query)
    if not entry:
        return None
    ts, found, payload = entry
    if _time.time() - ts > ttl_seconds:
        _cache.pop(query, None)
        return None
    if not found:
        return False
    return payload


def _cache_set(query: str, kind: str, found: bool, payload) -> None:
    import time as _time
    _cache[query] = (_time.time(), found, payload)


def search_artist_cached(artist_name: str) -> Optional[dict]:
    """Resolve an artist name to a MusicBrainz artist (cached, negative cache
    for names MusicBrainz does not know — i.e. most regional artists)."""
    query = "artist|" + _norm(artist_name)
    hit = _cache_get(query, "artist", _TTL["found"])
    if hit is not False and hit is not None:
        return hit
    miss = _cache_get(query, "artist", _TTL["notfound"])
    if miss is False:
        return None

    data = _mb_get("/artist", {"query": f'artist:"{artist_name}"', "limit": 5, "fmt": "json"})
    if not data:
        return None  # transient failure — fail soft, retry next time
    artists = data.get("artists") or []
    # normalized exact-name match first, then alias/score fallback
    target = _norm(artist_name)
    for a in artists:
        if _norm(a.get("name", "")) == target:
            _cache_set(query, "artist", True, {"id": a["id"], "name": a.get("name", "")})
            return {"id": a["id"], "name": a.get("name", "")}
    for a in artists:
        aliases = [x.get("name", "") for x in (a.get("aliases") or [])]
        if any(_norm(x) == target for x in aliases):
            _cache_set(query, "artist", True, {"id": a["id"], "name": a.get("name", "")})
            return {"id": a["id"], "name": a.get("name", "")}
    if artists:
        # best score fallback but require a strong name overlap
        best = artists[0]
        if best.get("score", 0) >= 80:
            _cache_set(query, "artist", True, {"id": best["id"], "name": best.get("name", "")})
            return {"id": best["id"], "name": best.get("name", "")}
    _cache_set(query, "artist", False, None)  # negative cache: not in MusicBrainz
    return None


def _fetch_release_groups(mb_artist_id: str) -> list:
    query = "rg|" + mb_artist_id
    cached = _cache_get(query, "album", _TTL["found"])
    if cached is not False and cached is not None:
        return cached
    data = _mb_get(
        f"/artist/{mb_artist_id}",
        {"inc": "release-groups", "fmt": "json"},
    )
    if not data:
        _cache_set(query, "album", False, None)
        return []
    rgs = [
        {
            "id": rg.get("id", ""),
            "title": rg.get("title", ""),
            "year": _first_release_year(rg.get("first-release-date")),
            "type": (rg.get("primary-type") or "").lower(),
        }
        for rg in (data.get("release-groups") or [])
    ]
    _cache_set(query, "album", True, rgs)
    return rgs


def _first_release_year(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    m = re.match(r"(\d{4})", date_str)
    return int(m.group(1)) if m else None


def _type_compatible(deezer_type: Optional[str], mb_type: Optional[str]) -> bool:
    dz = (deezer_type or "").lower()
    mb = (mb_type or "").lower()
    if not dz or not mb:
        return True
    if dz == mb:
        return True
    groups = {
        "album": {"album", "ep"},
        "ep": {"album", "ep"},
        "single": {"single"},
        "compilation": {"compilation", "album"},
    }
    return mb in groups.get(dz, set())


def enrich_album(artist_name: str, album_title: str, album_year: Optional[int],
                 deezer_type: Optional[str]) -> Optional[dict]:
    """Enrich a single Deezer album with MusicBrainz data on a confident match.
    Returns {"mb_release_group_id", "mb_title", "mb_year"} or None."""
    mb_artist = search_artist_cached(artist_name)
    if not mb_artist:
        return None  # regional artist: negative-cached, never probed per-album
    rgs = _fetch_release_groups(mb_artist["id"])
    if not rgs:
        return None
    target = _norm(album_title)
    for rg in rgs:
        if _norm(rg["title"]) != target:
            continue
        # hard gate: normalized title equality; year ±1; type compatibility
        year_ok = not album_year or not rg["year"] or abs(int(rg["year"]) - int(album_year)) <= 1
        type_ok = _type_compatible(deezer_type, rg["type"])
        if year_ok and type_ok:
            return {
                "mb_release_group_id": rg["id"],
                "mb_title": rg["title"],
                "mb_year": rg["year"],
            }
    return None


def enrich_albums(artist_name: str, albums: list) -> None:
    """Enrich a list of Deezer album dicts in place (adds mb_* keys)."""
    mb_artist = search_artist_cached(artist_name)
    if not mb_artist:
        return
    rgs = _fetch_release_groups(mb_artist["id"])
    if not rgs:
        return
    by_norm = {}
    for rg in rgs:
        by_norm.setdefault(_norm(rg["title"]), []).append(rg)
    for album in albums:
        title = album.get("title") or ""
        candidates = by_norm.get(_norm(title), [])
        if not candidates:
            continue
        year = album.get("year")
        dz_type = album.get("record_type")
        for rg in candidates:
            year_ok = not year or not rg["year"] or abs(int(rg["year"]) - int(year)) <= 1
            type_ok = _type_compatible(dz_type, rg["type"])
            if year_ok and type_ok:
                album["mb_release_group_id"] = rg["id"]
                album["mb_title"] = rg["title"]
                album["mb_year"] = rg["year"]
                break
