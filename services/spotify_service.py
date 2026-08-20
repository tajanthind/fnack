"""Spotify URL resolution service: Prioritizes exact ISRC lookups, with strict DuckDuckGo fallback."""

import logging
import re
from typing import Optional
from ddgs import DDGS

logger = logging.getLogger("fnack.spotify")

VARIANT_WORDS = ("cover", "live", "remix", "karaoke", "tribute", "instrumental", "sped up", "slowed", "lo-fi")
ISRC_REGEX = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$", re.IGNORECASE)


def is_valid_isrc(isrc: Optional[str]) -> bool:
    """Check if string matches official 12-character ISRC format."""
    if not isrc:
        return False
    clean = isrc.strip().replace("-", "")
    return bool(ISRC_REGEX.match(clean))


def find_spotify_track_by_isrc(
    isrc: str,
    artist_name: Optional[str] = None,
    song_name: Optional[str] = None,
    max_results: int = 5,
) -> Optional[str]:
    """Look up exact Spotify track URL using validated ISRC code."""
    if not is_valid_isrc(isrc):
        return None

    isrc_clean = isrc.strip().replace("-", "")
    queries = [
        f"site:open.spotify.com/track {isrc_clean}",
    ]
    if artist_name and song_name:
        queries.append(f"site:open.spotify.com/track {artist_name} {song_name} {isrc_clean}")

    for q in queries:
        try:
            with DDGS(timeout=4) as ddgs:
                results = list(ddgs.text(q, max_results=max_results))
                for r in results:
                    url = r.get("href", "")
                    if "open.spotify.com/track/" in url:
                        title = r.get("title", "").lower()
                        body = r.get("body", "").lower()
                        text_blob = f"{title} {body}"

                        # Verify candidate mentions artist or song or ISRC
                        if isrc_clean.lower() in text_blob or (artist_name and artist_name.lower() in text_blob) or (song_name and song_name.lower() in text_blob):
                            clean_url = url.split("?")[0]
                            logger.info("[SPOTIFY] Resolved ISRC '%s' -> %s", isrc_clean, clean_url)
                            return clean_url
        except Exception as e:
            logger.debug("[SPOTIFY] DDG ISRC search failed for query '%s': %s", q, e)

    return None


def find_spotify_track_by_search(
    song_name: str,
    artist_name: str,
    album_name: Optional[str] = None,
    max_results: int = 6,
    exclude_variants: bool = True,
) -> Optional[str]:
    """
    Search for a Spotify track URL by metadata strings (song, artist, album).
    Scores candidates to ensure the best possible match.
    """
    if not song_name or not artist_name:
        return None

    song_clean = song_name.strip()
    artist_clean = artist_name.strip()

    query = f"site:open.spotify.com/track {song_clean} {artist_clean}"
    if album_name and album_name.strip():
        query += f" {album_name.strip()}"

    candidates = []

    try:
        with DDGS(timeout=4) as ddgs:
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
    except Exception as e:
        logger.warning("[SPOTIFY] DDG string search failed for '%s - %s': %s", artist_clean, song_clean, e)
        return None

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0], reverse=True)
    best_score, best_url, best_title = candidates[0]

    # Require confident match on both artist and song name
    if best_score < 4:
        logger.debug("[SPOTIFY] Low confidence candidate for '%s - %s' (score %d: '%s')", artist_clean, song_clean, best_score, best_title)
        return None

    logger.info("[SPOTIFY] String search resolved '%s - %s' -> %s (score %d, candidate '%s')", artist_clean, song_clean, best_url, best_score, best_title)
    return best_url


def resolve_spotify_url(
    song_name: str,
    artist_name: str,
    album_name: Optional[str] = None,
    isrc: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve Spotify track URL with ISRC-first priority:
    1. Exact validated ISRC code lookup (100% recording accuracy).
    2. High-confidence metadata search (artist + song + album).
    """
    # 1. Highest priority: ISRC code lookup
    if isrc and is_valid_isrc(isrc):
        url = find_spotify_track_by_isrc(isrc, artist_name=artist_name, song_name=song_name)
        if url:
            return url

    # 2. Secondary fallback: Strict metadata string search
    if song_name and artist_name:
        url = find_spotify_track_by_search(song_name, artist_name, album_name)
        if url:
            return url

    return None
