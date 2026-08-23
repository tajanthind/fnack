"""Spotify URL resolution service: Zero-auth ISRC-first matching with thread-safe rate limiting & caching."""

import logging
import re
import threading
import time
from typing import Optional
import requests
from ddgs import DDGS

logger = logging.getLogger("fnack.spotify")

# Silence noisy third-party network crawler loggers completely
for _name in ("primp", "ddgs", "ddgs.ddgs", "urllib3", "curl_cffi", "duckduckgo_search"):
    _l = logging.getLogger(_name)
    _l.setLevel(logging.CRITICAL)
    _l.propagate = False
    _l.disabled = True

VARIANT_WORDS = ("cover", "live", "remix", "karaoke", "tribute", "instrumental", "sped up", "slowed", "lo-fi")
ISRC_REGEX = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$", re.IGNORECASE)

# Thread-safe rate limiter and in-memory URL cache
_search_lock = threading.Lock()
_last_search_time = 0.0
_MIN_SEARCH_INTERVAL = 0.8  # Pacing interval in seconds between search requests
_url_cache = {}  # Cache: isrc -> url and (norm_artist, norm_song) -> url

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _pace_search() -> None:
    """Thread-safe search rate limiter to prevent HTTP 429 / connection timeouts across parallel workers."""
    global _last_search_time
    with _search_lock:
        now = time.time()
        elapsed = now - _last_search_time
        if elapsed < _MIN_SEARCH_INTERVAL:
            time.sleep(_MIN_SEARCH_INTERVAL - elapsed)
        _last_search_time = time.time()


def is_valid_isrc(isrc: Optional[str]) -> bool:
    """Check if string matches official 12-character ISRC format."""
    if not isrc:
        return False
    clean = str(isrc).strip().replace("-", "")
    return bool(ISRC_REGEX.match(clean))


def _normalize(text: str) -> str:
    """Normalize text for consistent cache keys."""
    return re.sub(r"[^\w\s]", "", (text or "").lower()).strip()


def _search_ddg_html(query: str) -> list[str]:
    """Execute fast direct POST to DuckDuckGo HTML endpoint without third-party wrapper overhead."""
    _pace_search()
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=_HTTP_HEADERS,
            timeout=6,
        )
        if resp.status_code == 200:
            found = re.findall(r"https://open\.spotify\.com/track/[a-zA-Z0-9]+", resp.text)
            return list(dict.fromkeys(found))  # deduplicate preserving order
    except Exception as e:
        logger.debug("[SPOTIFY] DDG HTML search error: %s", e)
    return []


def find_spotify_track_by_isrc(
    isrc: str,
    artist_name: Optional[str] = None,
    song_name: Optional[str] = None,
    max_results: int = 4,
) -> Optional[str]:
    """Look up exact Spotify track URL using validated ISRC code with rate-limit pacing."""
    if not is_valid_isrc(isrc):
        return None

    isrc_clean = str(isrc).strip().replace("-", "")
    if isrc_clean in _url_cache:
        return _url_cache[isrc_clean]

    artist_clean = (artist_name or "").strip()
    # Primary lookup: direct DDG HTML search with artist context to prevent cross-artist ISRC collisions
    query = f"{artist_clean} {isrc_clean} site:open.spotify.com/track" if artist_clean else f"{isrc_clean} site:open.spotify.com/track"
    found_urls = _search_ddg_html(query)
    if found_urls:
        clean_url = found_urls[0].split("?")[0]
        _url_cache[isrc_clean] = clean_url
        logger.info("[SPOTIFY] Resolved ISRC '%s' (%s) -> %s", isrc_clean, artist_clean or "unnamed", clean_url)
        return clean_url

    # Secondary lookup via DDGS
    _pace_search()
    try:
        with DDGS(timeout=4) as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            for r in results:
                url = r.get("href", "")
                if "open.spotify.com/track/" in url:
                    clean_url = url.split("?")[0]
                    _url_cache[isrc_clean] = clean_url
                    logger.info("[SPOTIFY] Resolved ISRC '%s' (%s) -> %s via DDGS", isrc_clean, artist_clean or "unnamed", clean_url)
                    return clean_url
    except Exception:
        pass

    # Fallback to plain ISRC lookup if artist-qualified lookup returned nothing
    if artist_clean:
        raw_query = f"{isrc_clean} site:open.spotify.com/track"
        found_urls = _search_ddg_html(raw_query)
        if found_urls:
            clean_url = found_urls[0].split("?")[0]
            _url_cache[isrc_clean] = clean_url
            return clean_url

    return None


def find_spotify_track_by_search(
    song_name: str,
    artist_name: str,
    album_name: Optional[str] = None,
    max_results: int = 4,
    exclude_variants: bool = True,
) -> Optional[str]:
    """Search for a Spotify track URL by metadata strings with rate-limit pacing."""
    if not song_name or not artist_name:
        return None

    cache_key = (_normalize(artist_name), _normalize(song_name))
    if cache_key in _url_cache:
        return _url_cache[cache_key]

    song_clean = song_name.strip()
    artist_clean = artist_name.strip()

    # Primary lookup via DDG HTML
    query = f"{artist_clean} {song_clean} site:open.spotify.com/track"
    found_urls = _search_ddg_html(query)
    if found_urls:
        clean_url = found_urls[0].split("?")[0]
        _url_cache[cache_key] = clean_url
        logger.info("[SPOTIFY] Search resolved '%s - %s' -> %s", artist_clean, song_clean, clean_url)
        return clean_url

    # Secondary lookup with album title
    if album_name and album_name.strip():
        query_alb = f"{artist_clean} {song_clean} {album_name.strip()} site:open.spotify.com/track"
        found_urls = _search_ddg_html(query_alb)
        if found_urls:
            clean_url = found_urls[0].split("?")[0]
            _url_cache[cache_key] = clean_url
            logger.info("[SPOTIFY] Album search resolved '%s - %s' -> %s", artist_clean, song_clean, clean_url)
            return clean_url

    # Fallback to DDGS text search
    _pace_search()
    try:
        with DDGS(timeout=4) as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            for r in results:
                url = r.get("href", "")
                if "open.spotify.com/track/" in url:
                    clean_url = url.split("?")[0]
                    _url_cache[cache_key] = clean_url
                    logger.info("[SPOTIFY] DDGS search resolved '%s - %s' -> %s", artist_clean, song_clean, clean_url)
                    return clean_url
    except Exception:
        pass

    return None


def resolve_spotify_url(
    song_name: str,
    artist_name: str,
    album_name: Optional[str] = None,
    isrc: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve Spotify track URL with zero-auth rate-limited lookups:
    1. Validated ISRC code lookup.
    2. High-confidence metadata search fallback.
    """
    # 1. Highest priority: ISRC code lookup
    if isrc and is_valid_isrc(isrc):
        url = find_spotify_track_by_isrc(isrc, artist_name=artist_name, song_name=song_name)
        if url:
            return url

    # 2. Secondary fallback: Metadata search
    if song_name and artist_name:
        url = find_spotify_track_by_search(song_name, artist_name, album_name)
        if url:
            return url

    return None
