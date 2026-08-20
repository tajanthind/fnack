"""Spotify URL resolution service: Zero-auth ISRC-first matching with thread-safe rate limiting & caching."""

import logging
import re
import threading
import time
from typing import Optional
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
_MIN_SEARCH_INTERVAL = 1.2  # Pacing interval in seconds between search requests to prevent 429 rate-limiting
_url_cache = {}  # Cache: isrc -> url and (norm_artist, norm_song) -> url


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
    clean = isrc.strip().replace("-", "")
    return bool(ISRC_REGEX.match(clean))


def _normalize(text: str) -> str:
    """Normalize text for consistent cache keys."""
    return re.sub(r"[^\w\s]", "", (text or "").lower()).strip()


def find_spotify_track_by_isrc(
    isrc: str,
    artist_name: Optional[str] = None,
    song_name: Optional[str] = None,
    max_results: int = 4,
) -> Optional[str]:
    """Look up exact Spotify track URL using validated ISRC code with rate-limit pacing."""
    if not is_valid_isrc(isrc):
        return None

    isrc_clean = isrc.strip().replace("-", "")
    if isrc_clean in _url_cache:
        return _url_cache[isrc_clean]

    _pace_search()
    query = f"site:open.spotify.com/track {isrc_clean}"

    try:
        with DDGS(timeout=3) as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            for r in results:
                url = r.get("href", "")
                if "open.spotify.com/track/" in url:
                    title = r.get("title", "").lower()
                    body = r.get("body", "").lower()
                    text_blob = f"{title} {body}"

                    if isrc_clean.lower() in text_blob or (artist_name and artist_name.lower() in text_blob) or (song_name and song_name.lower() in text_blob):
                        clean_url = url.split("?")[0]
                        _url_cache[isrc_clean] = clean_url
                        logger.info("[SPOTIFY] Resolved ISRC '%s' -> %s", isrc_clean, clean_url)
                        return clean_url
    except Exception:
        pass

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

    _pace_search()
    query = f"site:open.spotify.com/track {song_clean} {artist_clean}"
    if album_name and album_name.strip():
        query += f" {album_name.strip()}"

    candidates = []

    try:
        with DDGS(timeout=3) as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            for r in results:
                url = r.get("href", "")
                if "open.spotify.com/track/" not in url:
                    continue

                title = r.get("title", "")
                body = r.get("body", "")
                text_blob = f"{title} {body}".lower()

                score = 0
                if artist_clean.lower() in text_blob:
                    score += 3
                if song_clean.lower() in text_blob:
                    score += 2
                if album_name and album_name.strip().lower() in text_blob:
                    score += 1
                if exclude_variants and any(w in text_blob for w in VARIANT_WORDS):
                    score -= 3

                candidates.append((score, url.split("?")[0], title))
    except Exception:
        return None

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0], reverse=True)
    best_score, best_url, best_title = candidates[0]

    if best_score < 4:
        return None

    _url_cache[cache_key] = best_url
    logger.info("[SPOTIFY] Search resolved '%s - %s' -> %s", artist_clean, song_clean, best_url)
    return best_url


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
