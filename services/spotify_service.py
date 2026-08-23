"""Spotify URL resolution service: Zero-auth matching with thread-safe rate limiting, title verification & album parsing."""

import logging
import re
import threading
import time
import unicodedata
from typing import Optional, Tuple
import requests

logger = logging.getLogger("fnack.spotify")

# Silence noisy third-party loggers
for _name in ("primp", "ddgs", "ddgs.ddgs", "urllib3", "curl_cffi", "duckduckgo_search"):
    _l = logging.getLogger(_name)
    _l.setLevel(logging.CRITICAL)
    _l.propagate = False
    _l.disabled = True

ISRC_REGEX = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$", re.IGNORECASE)
SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com(?:/intl-[a-z]+)?/track/([a-zA-Z0-9]{22})", re.IGNORECASE)
SPOTIFY_ALBUM_RE = re.compile(r"open\.spotify\.com(?:/intl-[a-z]+)?/album/([a-zA-Z0-9]{22})", re.IGNORECASE)

# Thread-safe rate limiter and in-memory URL cache
_search_lock = threading.Lock()
_last_search_time = 0.0
_MIN_SEARCH_INTERVAL = 0.5  # Pacing interval in seconds between search requests
_url_cache = {}  # Cache: isrc -> url and (norm_artist, norm_song) -> url
_album_tracks_cache = {}  # Cache: (norm_artist, norm_album) -> dict[norm_title, url]
_yandex_engine = None


def _get_yandex_engine():
    global _yandex_engine
    if _yandex_engine is None:
        try:
            from ddgs.engines.yandex import Yandex
            _yandex_engine = Yandex()
        except Exception as e:
            logger.debug("[SPOTIFY] Yandex engine init note: %s", e)
    return _yandex_engine


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
    """Normalize text for consistent cache keys and comparison."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", text)
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def _clean_track_title(title: str) -> str:
    """Remove extraneous video/edition/soundtrack markers that degrade search accuracy."""
    if not title:
        return ""
    t = re.sub(
        r"[\(\[\{](?:from\s+[\"\'\w\s]+|remaster|deluxe|bonus|anniversary|live|explicit|feat\.|ft\.|official|audio|video|edit|mono|stereo|original\s+motion\s+picture\s+soundtrack|soundtrack|old\s+version)[^\)\]\}]*[\)\]\}]",
        "",
        title,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"\s*-\s*(?:remaster|live|mono|stereo|radio edit|single version|album version|panjab intro).*$",
        "",
        t,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", t).strip()


def _is_title_match(song_name: str, result_title: str) -> bool:
    """Check if search result title corresponds to the requested track title."""
    if not song_name or not result_title:
        return False
    norm_song = _normalize(song_name)
    norm_res = _normalize(result_title)
    if not norm_song or not norm_res:
        return False
    if norm_song == norm_res or norm_song in norm_res or norm_res in norm_song:
        return True
    words = [w for w in norm_song.split() if len(w) > 2]
    if words and all(w in norm_res for w in words):
        return True
    if len(words) >= 2 and sum(1 for w in words if w in norm_res) >= len(words) * 0.7:
        return True
    return False


def _search_ddgs_candidates(query: str) -> list[Tuple[str, str]]:
    """Execute search query and return list of (clean_spotify_url, result_title) pairs."""
    _pace_search()
    candidates = []
    engine = _get_yandex_engine()
    if engine:
        try:
            results = engine.search(query)
            for r in (results or []):
                href = getattr(r, "href", "") or ""
                m = SPOTIFY_TRACK_RE.search(href)
                if m:
                    clean_url = f"https://open.spotify.com/track/{m.group(1)}"
                    title = getattr(r, "title", "") or ""
                    if not any(c[0] == clean_url for c in candidates):
                        candidates.append((clean_url, title))
        except Exception as e:
            logger.debug("[SPOTIFY] Yandex search query '%s' note: %s", query, e)

    if not candidates:
        _pace_search()
        try:
            from ddgs import DDGS
            with DDGS(timeout=4) as ddgs:
                results = list(ddgs.text(query, max_results=5))
                for r in results:
                    href = r.get("href", "") or ""
                    m = SPOTIFY_TRACK_RE.search(href)
                    if m:
                        clean_url = f"https://open.spotify.com/track/{m.group(1)}"
                        title = r.get("title", "") or ""
                        if not any(c[0] == clean_url for c in candidates):
                            candidates.append((clean_url, title))
        except Exception:
            pass

    return candidates


def _resolve_album_tracks_from_spotify(artist_name: str, album_name: str) -> dict[str, str]:
    """Search for Spotify album page and extract mapping of track title & index -> track URL."""
    cache_key = (_normalize(artist_name), _normalize(album_name))
    if cache_key in _album_tracks_cache:
        return _album_tracks_cache[cache_key]

    clean_art = (artist_name or "").strip()
    clean_alb = (album_name or "").strip()
    if not clean_art or not clean_alb:
        return {}

    queries = [
        f'"{clean_art}" "{clean_alb}" site:open.spotify.com/album',
        f"{clean_art} {clean_alb} site:open.spotify.com/album",
        f"{clean_art} {clean_alb} spotify album",
    ]

    album_url = None
    engine = _get_yandex_engine()
    for q in queries:
        _pace_search()
        if engine:
            try:
                results = engine.search(q)
                for r in (results or []):
                    href = getattr(r, "href", "") or ""
                    m = SPOTIFY_ALBUM_RE.search(href)
                    if m:
                        album_url = f"https://open.spotify.com/album/{m.group(1)}"
                        break
            except Exception:
                pass
        if album_url:
            break

    track_map = {}
    if album_url:
        try:
            import html as html_module
            resp = requests.get(album_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=10)
            if resp.status_code == 200:
                blocks = re.findall(r'href=\"/track/([a-zA-Z0-9]{22})\"[^>]*>([\s\S]*?)</a>', resp.text)
                seen_ids = set()
                idx = 1
                for tid, content in blocks:
                    if tid not in seen_ids:
                        seen_ids.add(tid)
                        t_url = f"https://open.spotify.com/track/{tid}"
                        title = html_module.unescape(re.sub(r'<[^>]+>', '', content).strip())
                        norm_t = _normalize(title)
                        if norm_t:
                            track_map[norm_t] = t_url
                        track_map[f"__idx_{idx}__"] = t_url
                        idx += 1
        except Exception as e:
            logger.debug("[SPOTIFY] Album track parse note for %s: %s", album_url, e)

    _album_tracks_cache[cache_key] = track_map
    return track_map


def find_spotify_track_by_isrc(
    isrc: str,
    artist_name: Optional[str] = None,
    song_name: Optional[str] = None,
) -> Optional[str]:
    """Look up exact Spotify track URL using validated ISRC code with title verification."""
    if not is_valid_isrc(isrc):
        return None

    isrc_clean = str(isrc).strip().replace("-", "").upper()
    if isrc_clean in _url_cache:
        return _url_cache[isrc_clean]

    artist_clean = (artist_name or "").strip()
    song_clean = _clean_track_title(song_name or "").strip()

    queries = []
    if isrc_clean:
        queries.append(f'"{isrc_clean}" site:open.spotify.com/track')
        queries.append(f'"{isrc_clean}" spotify track')
    if artist_clean:
        queries.append(f'"{artist_clean}" "{isrc_clean}" site:open.spotify.com/track')
    if artist_clean and song_clean:
        queries.append(f'"{artist_clean}" "{song_clean}" site:open.spotify.com/track')
        queries.append(f'"{song_clean}" "{artist_clean}" site:open.spotify.com/track')

    for q in queries:
        candidates = _search_ddgs_candidates(q)
        for clean_url, res_title in candidates:
            if not song_clean or _is_title_match(song_clean, res_title):
                _url_cache[isrc_clean] = clean_url
                if artist_clean and song_clean:
                    _url_cache[(_normalize(artist_clean), _normalize(song_clean))] = clean_url
                logger.info("[SPOTIFY] Resolved ISRC '%s' (%s - %s) -> %s", isrc_clean, artist_clean, song_clean, clean_url)
                return clean_url

    return None


def find_spotify_track_by_search(
    song_name: str,
    artist_name: str,
    album_name: Optional[str] = None,
    track_number: Optional[int] = None,
) -> Optional[str]:
    """Search for a Spotify track URL with candidate title verification and album fallback."""
    if not song_name or not artist_name:
        return None

    clean_art = (artist_name or "").strip()
    clean_song = _clean_track_title(song_name or "").strip()
    clean_alb = (album_name or "").strip()

    cache_key = (_normalize(clean_art), _normalize(clean_song))
    if cache_key in _url_cache:
        return _url_cache[cache_key]

    # Tier 1: Direct track search queries with title verification
    queries = [
        f'"{clean_art}" "{clean_song}" site:open.spotify.com/track',
        f'"{clean_song}" "{clean_art}" site:open.spotify.com/track',
        f"{clean_art} {clean_song} site:open.spotify.com/track",
        f"{clean_art} {clean_song} open.spotify.com/track",
    ]
    if clean_alb:
        queries.append(f"{clean_art} {clean_song} {clean_alb} site:open.spotify.com/track")

    for q in queries:
        candidates = _search_ddgs_candidates(q)
        for clean_url, res_title in candidates:
            if _is_title_match(clean_song, res_title):
                _url_cache[cache_key] = clean_url
                logger.info("[SPOTIFY] Zero-auth resolved '%s - %s' -> %s (Title: '%s')", clean_art, clean_song, clean_url, res_title)
                return clean_url

    # Tier 2: Album page extraction fallback (matches by track title)
    if clean_alb:
        album_tracks = _resolve_album_tracks_from_spotify(clean_art, clean_alb)
        # Check title match in album tracks
        for t_norm, t_url in album_tracks.items():
            if t_norm.startswith("__idx_"):
                continue
            if _is_title_match(clean_song, t_norm):
                _url_cache[cache_key] = t_url
                logger.info("[SPOTIFY] Resolved via album '%s' title match '%s' -> %s", clean_alb, t_norm, t_url)
                return t_url

        # For single-track releases (e.g. singles/EPs), match if single title matches album
        if track_number == 1 and len([k for k in album_tracks if not k.startswith("__idx_")]) == 1:
            only_title = [k for k in album_tracks if not k.startswith("__idx_")][0]
            if _is_title_match(clean_song, only_title) or _is_title_match(clean_alb, only_title):
                url = album_tracks["__idx_1__"]
                _url_cache[cache_key] = url
                logger.info("[SPOTIFY] Resolved single '%s' -> %s", clean_alb, url)
                return url

    return None


_spotify_token: Optional[str] = None
_spotify_token_expiry: float = 0.0
_token_lock = threading.Lock()


def _get_spotify_api_token(client_id: str, client_secret: str) -> Optional[str]:
    """Retrieve Spotify API access token via client credentials flow (optional feature)."""
    global _spotify_token, _spotify_token_expiry
    now = time.time()
    with _token_lock:
        if _spotify_token and now < _spotify_token_expiry - 60:
            return _spotify_token

        try:
            from version import __version__
            ua = f"fnack/{__version__}"
        except Exception:
            ua = "fnack/0.2.02"

        try:
            resp = requests.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id.strip(), client_secret.strip()),
                headers={"User-Agent": ua},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                _spotify_token = data.get("access_token")
                _spotify_token_expiry = now + float(data.get("expires_in", 3600))
                return _spotify_token
        except Exception as e:
            logger.debug("[SPOTIFY] Optional Spotify API token error: %s", e)
        return None


def find_spotify_track_via_api(
    client_id: str,
    client_secret: str,
    isrc: Optional[str] = None,
    artist_name: Optional[str] = None,
    song_name: Optional[str] = None,
) -> Optional[str]:
    """Resolve track using official Spotify Web API with optional user credentials."""
    token = _get_spotify_api_token(client_id, client_secret)
    if not token:
        return None

    try:
        from version import __version__
        ua = f"fnack/{__version__}"
    except Exception:
        ua = "fnack/0.2.02"

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": ua,
    }

    if isrc and is_valid_isrc(isrc):
        isrc_clean = str(isrc).strip().replace("-", "").upper()
        if isrc_clean in _url_cache:
            return _url_cache[isrc_clean]
        try:
            resp = requests.get(
                "https://api.spotify.com/v1/search",
                params={"q": f"isrc:{isrc_clean}", "type": "track", "limit": 1},
                headers=headers,
                timeout=8,
            )
            if resp.status_code == 200:
                items = resp.json().get("tracks", {}).get("items", [])
                if items and items[0].get("external_urls", {}).get("spotify"):
                    url = items[0]["external_urls"]["spotify"]
                    _url_cache[isrc_clean] = url
                    logger.info("[SPOTIFY] Resolved ISRC '%s' -> %s via Spotify API", isrc_clean, url)
                    return url
        except Exception as e:
            logger.debug("[SPOTIFY] Spotify API search by ISRC error: %s", e)

    if artist_name and song_name:
        cache_key = (_normalize(artist_name), _normalize(song_name))
        if cache_key in _url_cache:
            return _url_cache[cache_key]
        try:
            clean_art = artist_name.strip()
            clean_song = _clean_track_title(song_name).strip()
            resp = requests.get(
                "https://api.spotify.com/v1/search",
                params={"q": f'artist:"{clean_art}" track:"{clean_song}"', "type": "track", "limit": 1},
                headers=headers,
                timeout=8,
            )
            if resp.status_code == 200:
                items = resp.json().get("tracks", {}).get("items", [])
                if items and items[0].get("external_urls", {}).get("spotify"):
                    url = items[0]["external_urls"]["spotify"]
                    _url_cache[cache_key] = url
                    logger.info("[SPOTIFY] Resolved '%s - %s' -> %s via Spotify API", clean_art, clean_song, url)
                    return url
        except Exception as e:
            logger.debug("[SPOTIFY] Spotify API search by track error: %s", e)

    return None


def resolve_spotify_url(
    song_name: str,
    artist_name: str,
    album_name: Optional[str] = None,
    isrc: Optional[str] = None,
    track_number: Optional[int] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve Spotify track URL without requiring any user account:
    1. Optional Spotify Web API if user explicitly configured credentials.
    2. Validated ISRC code lookup via zero-auth search with title verification.
    3. Multi-tier zero-auth DDGS/Yandex search matching with strict title verification.
    4. Album page track mapping fallback.
    """
    # 0. Optional API lookup if credentials provided
    import os
    cid = (client_id or os.environ.get("SPOTIFY_CLIENT_ID", "")).strip()
    csecret = (client_secret or os.environ.get("SPOTIFY_CLIENT_SECRET", "")).strip()
    if cid and csecret:
        url = find_spotify_track_via_api(cid, csecret, isrc=isrc, artist_name=artist_name, song_name=song_name)
        if url:
            return url

    # 1. ISRC code lookup (zero-auth with title match)
    if isrc and is_valid_isrc(isrc):
        url = find_spotify_track_by_isrc(isrc, artist_name=artist_name, song_name=song_name)
        if url:
            return url

    # 2. Metadata search (zero-auth with title match & album fallback)
    if song_name and artist_name:
        url = find_spotify_track_by_search(song_name, artist_name, album_name, track_number=track_number)
        if url:
            return url

    return None
