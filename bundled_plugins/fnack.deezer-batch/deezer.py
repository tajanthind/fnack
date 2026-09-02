"""Deezer public API service for fnack — metadata, artist search, and rate-limited discography ingestion."""

import logging
import re
import time
import unicodedata
from typing import Any, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("fnack.deezer")

DEEZER_API = "https://api.deezer.com"
TIMEOUT = 12

# Create persistent session with retry pooling
_session = requests.Session()
_retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
_adapter = HTTPAdapter(max_retries=_retries, pool_connections=10, pool_maxsize=20)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

# In-memory search cache with TTL
_search_cache: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL = 600  # 10 minutes


def _normalize(s: str) -> str:
    """Normalize a title/name for fuzzy comparison (identical helper to the
    one shared with the iTunes provider pre-extraction; plugins are
    standalone, so it lives here)."""
    if not s:
        return ""
    # Strip (feat. ...), [feat. ...], (with ...), [with ...], (Deluxe...), etc.
    s = re.sub(r"[\(\[\{]\s*(?:feat\.?|featuring|with|deluxe|remastered|explicit|clean)\b[^\)\]\}]*[\)\]\}]", "", s, flags=re.IGNORECASE)
    # Strip trailing - Single, - EP, (Single), (EP), etc.
    s = re.sub(r"\s*-\s*(Single|EP|Album|Compilation|Soundtrack)$", "", s, flags=re.IGNORECASE)
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-zA-Z0-9]+", "", s).lower()


def _get(url: str, params: dict[str, Any] | None = None) -> dict:
    max_retries = 4
    for attempt in range(max_retries):
        resp = _session.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            err_code = err.get("code")
            if err_code == 4 and attempt < max_retries - 1:
                time.sleep(1.2 * (attempt + 1))
                continue
            raise ValueError(f"Deezer API Error: {err.get('message', 'Unknown error')} (code {err_code})")
        return data
    return {}


def search_artist(query: str, limit: int = 10) -> list[dict]:
    """Search Deezer for artists with caching."""
    q = (query or "").strip().lower()
    if not q or len(q) < 2:
        return []

    now = time.time()
    if q in _search_cache:
        ts, cached = _search_cache[q]
        if now - ts < CACHE_TTL:
            return cached

    try:
        data = _get(f"{DEEZER_API}/search/artist", {"q": q, "limit": limit})
        norm_q = _normalize(q)
        results = [
            {
                "id": i["id"],
                "name": i["name"],
                "image_url": i.get("picture_medium") or i.get("picture_small"),
                "nb_album": i.get("nb_album", 0),
                "nb_fan": i.get("nb_fan", 0),
                "link": i.get("link"),
            }
            for i in data.get("data", [])
        ]
        # Sort prioritizing exact normalized name equality, then popularity
        results.sort(
            key=lambda x: (
                _normalize(x["name"]) == norm_q,
                x.get("nb_fan", 0),
                x.get("nb_album", 0),
            ),
            reverse=True,
        )
        _search_cache[q] = (now, results)
        return results
    except Exception as e:
        logger.warning("[DEEZER] Artist search failed for '%s': %s", query, e)
        return []


def get_artist_info(artist_id: int) -> dict:
    """Get basic artist details."""
    data = _get(f"{DEEZER_API}/artist/{artist_id}")
    return {
        "id": data["id"],
        "name": data.get("name", "Unknown Artist"),
        "image_url": data.get("picture_medium") or data.get("picture_big"),
        "nb_album": data.get("nb_album", 0),
        "nb_fan": data.get("nb_fan", 0),
    }


# Filter regex patterns
_RE_REMIX = re.compile(r"\b(remix|remixes|remixed|club mix|extended mix|vip mix|dub mix|edit)\b", re.IGNORECASE)
_RE_LOFI = re.compile(r"\b(lo-?fi|slowed|sped up|speed up|acoustic|instrumental|piano version|orchestral)\b", re.IGNORECASE)
_RE_LIVE = re.compile(r"\b(live|in concert|live at|live from|tour|unplugged)\b", re.IGNORECASE)
_RE_PLAYLIST_COMPILATION = re.compile(
    r"\b("
    r"mashup|mash-up|mega\s*mix|party\s*mix|workout\s*mix|dj\s*mix|non-?stop|dance\s*mix|"
    r"party\s*vibes|dance\s*floor|continuous\s*mix|hits\s*mix|best\s*of\b|greatest\s*hits|"
    r"top\s*\d+|top\s*hits|tribute\s*to|various\s*artists|compilation|diwali\s*hits|party\s*hits|"
    r"the\s*mega\s*mix|tik\s*tok\s*hits|slowed\s*\+\s*reverb|speed\s*up\s*remix|bass\s*boosted|"
    r"club\s*remixes|soundtrack\s*from|full\s*album|mixtape|bootleg|hits\s*collection|"
    r"top\s*tracks|dj\s*set|punjabi\s*mix|wedding\s*collection|wedding\s*special|"
    r"wedding\s*songs|wedding\s*hits|wedding\s*playlist|wedding\s*mashup|bollywood\s*wedding|"
    r"dil\s*se\s+\w+|essential|essentials|hits\s*of\b|all\s*time\s*hits|"
    r"golden\s*hits|evergreen\s*hits|super\s*hits|superhits|hit\s*machine|love\s*hits|"
    r"romantic\s*hits|sad\s*hits|party\s*songs|dance\s*party|dance\s*collection|"
    r"the\s*collection\b|pure\s+\w+|voice\s*of\s+\w+|timeless\s+\w+|anthology|"
    r"collection\b|mix\s*vol|workout\s*beats|workout\s*songs|quarantine\s*with|"
    r"in\s*love\s*with\s+\w+|hum\s*do\s*hamare|do\s*gabru"
    r")\b",
    re.IGNORECASE,
)


def _should_filter_title(
    title: str,
    filter_remixes: bool,
    filter_lofi: bool,
    filter_live: bool,
    filter_compilations: bool = False,
) -> bool:
    """Check if title matches any active exclusion filters."""
    t = title.lower()
    if filter_remixes and _RE_REMIX.search(t):
        return True
    if filter_lofi and _RE_LOFI.search(t):
        return True
    if filter_live and _RE_LIVE.search(t):
        return True
    if filter_compilations and _RE_PLAYLIST_COMPILATION.search(t):
        return True
    return False


def get_artist_discography(
    artist_id: int,
    filter_remixes: bool = True,
    filter_lofi: bool = True,
    filter_live: bool = True,
    filter_compilations: bool = True,
    include_albums: bool = True,
    include_singles: bool = True,
    include_compilations: bool = False,
    sleep_delay: float = 0.15,
) -> dict:
    """
    Fetch complete artist discography with tracks and ISRCs, rate-limited and filtered.
    Ensures albums where artist was only a featured guest are not duplicated across artists.
    """
    artist_info = get_artist_info(artist_id)
    artist_name = artist_info["name"]
    artist_image = artist_info["image_url"]

    raw_albums = []
    seen_album_ids = set()
    url: str | None = f"{DEEZER_API}/artist/{artist_id}/albums?limit=100"

    while url:
        try:
            data = _get(url)
            for a in data.get("data", []):
                aid = a["id"]
                if aid in seen_album_ids:
                    continue
                seen_album_ids.add(aid)
                raw_albums.append(a)
            url = data.get("next")
            if url:
                time.sleep(sleep_delay)
        except Exception as e:
            logger.warning("[DEEZER] Error fetching album page for artist %d: %s", artist_id, e)
            break

    logger.info("[DEEZER] Found %d raw releases for '%s'", len(raw_albums), artist_name)

    filtered_albums = []
    norm_artist_name = _normalize(artist_name)

    for a in raw_albums:
        rec_type = (a.get("record_type") or "album").lower()
        title = a.get("title", "")

        # Type filter
        if rec_type == "album" and not include_albums:
            continue
        if rec_type in ("single", "ep") and not include_singles:
            continue
        if rec_type == "compile" and not include_compilations:
            continue
        if filter_compilations and rec_type == "compile":
            continue

        # Mashup, playlist & title keyword filter for album
        if _should_filter_title(title, filter_remixes, filter_lofi, filter_live, filter_compilations):
            continue

        if filter_compilations and rec_type in ("album", "compile") and re.search(r"\b(mix|collection|workout|wedding|hits\s*of)\b", title, re.IGNORECASE):
            continue

        # Fetch detailed track list with ISRCs
        album_id = a["id"]
        tracks = []
        try:
            time.sleep(sleep_delay)
            album_detail = _get(f"{DEEZER_API}/album/{album_id}")

            # Primary artist check: if album detail specifies a different primary artist (and not a compilation we want), skip it
            album_artist_obj = album_detail.get("artist") or {}
            album_artist_id = album_artist_obj.get("id")
            album_artist_name = album_artist_obj.get("name", "")

            if album_artist_name and not include_compilations:
                norm_alb_art = _normalize(album_artist_name)
                if norm_alb_art in ("variousartists", "various", "variousartist") or "various artists" in album_artist_name.lower():
                    logger.debug("[DEEZER] Skipping Various Artists compilation '%s'", title)
                    continue
                if album_artist_id and album_artist_id != artist_id and norm_artist_name != norm_alb_art:
                    logger.debug("[DEEZER] Skipping release '%s' - primary artist is '%s' (id %s != %s)", title, album_artist_name, album_artist_id, artist_id)
                    continue

            tracks_data = album_detail.get("tracks", {}).get("data", [])
            if not tracks_data:
                time.sleep(sleep_delay)
                tracks_data = _get(f"{DEEZER_API}/album/{album_id}/tracks", {"limit": 100}).get("data", [])

            # Multi-artist compilation check: if track details show different artists on most tracks
            if len(tracks_data) >= 4 and not include_compilations:
                artist_track_count = sum(
                    1 for t in tracks_data
                    if not (t.get("artist") or {}).get("name") or _normalize((t.get("artist") or {}).get("name", "")) == norm_artist_name
                )
                if (artist_track_count / len(tracks_data)) < 0.5:
                    logger.debug("[DEEZER] Skipping multi-artist compilation '%s' (%d/%d tracks by '%s')", title, artist_track_count, len(tracks_data), artist_name)
                    continue

            for t in tracks_data:
                track_title = t.get("title", "")
                # Filter track if individual title is remix/lofi/live
                if _should_filter_title(track_title, filter_remixes, filter_lofi, filter_live):
                    continue

                tracks.append({
                    "id": t["id"],
                    "title": track_title,
                    "duration": float(t.get("duration", 0)),
                    "track_position": t.get("track_position", len(tracks) + 1),
                    "disk_number": t.get("disk_number", 1),
                    "isrc": t.get("isrc") if t.get("isrc") and len(str(t.get("isrc")).strip()) == 12 else None,
                    "genre": _extract_genre(t),
                })
        except Exception as e:
            logger.warning("[DEEZER] Failed fetching track details for album %d (%s): %s", album_id, title, e)

        if not tracks:
            continue  # Skip empty album if all tracks were filtered out

        year_val = None
        rel_date = a.get("release_date") or ""
        if len(rel_date) >= 4 and rel_date[:4].isdigit():
            year_val = int(rel_date[:4])

        filtered_albums.append({
            "id": album_id,
            "title": title,
            "record_type": rec_type,
            "cover_url": a.get("cover_medium") or a.get("cover_big"),
            "year": year_val,
            "tracks": tracks,
            "track_count": len(tracks),
        })

    # Complement with iTunes discography for releases missing on Deezer (e.g.
    # Enigma). The iTunes implementation lives in the fnack.itunes plugin
    # (Phase 4 extraction); the import is lazy + guarded so this provider
    # works standalone and the complementary sync degrades gracefully when
    # the itunes plugin is not installed.
    try:
        import itunes as _itunes  # sibling plugin module (multi-file import)
        album_by_norm: dict[str, dict] = {_itunes._normalize(alb["title"]): alb for alb in filtered_albums if alb.get("title")}
        existing_titles = {alb["title"] for alb in filtered_albums}
        itunes_albums = _itunes.get_itunes_artist_albums(
            artist_name=artist_name,
            filter_remixes=filter_remixes,
            filter_lofi=filter_lofi,
            filter_live=filter_live,
            filter_compilations=filter_compilations,
            include_albums=include_albums,
            include_singles=include_singles,
            include_compilations=include_compilations,
            existing_album_titles=existing_titles,
        )
        for ia in itunes_albums:
            norm_title = _normalize(ia["title"])
            existing_alb = album_by_norm.get(norm_title)
            if existing_alb is None:
                i_tracks = _itunes.get_itunes_album_tracks(ia["itunes_id"])
                if not i_tracks:
                    continue
                new_alb = {
                    "id": f"itunes_{ia['itunes_id']}",
                    "title": ia["title"],
                    "record_type": ia["record_type"],
                    "cover_url": ia["cover_url"],
                    "year": ia["year"],
                    "tracks": i_tracks,
                    "track_count": len(i_tracks),
                }
                filtered_albums.append(new_alb)
                album_by_norm[norm_title] = new_alb
                logger.info("[DISCOGRAPHY] Added missing release from iTunes: '%s' (%s, %d tracks)", ia["title"], ia["year"], len(i_tracks))
            elif len(existing_alb.get("tracks", [])) <= 1 and ia.get("track_count", 0) > len(existing_alb.get("tracks", [])):
                # Upgrade partial/sample release with full tracklist from iTunes
                i_tracks = _itunes.get_itunes_album_tracks(ia["itunes_id"])
                if len(i_tracks) > len(existing_alb.get("tracks", [])):
                    existing_alb["tracks"] = i_tracks
                    existing_alb["track_count"] = len(i_tracks)
                    if not existing_alb.get("cover_url") and ia.get("cover_url"):
                        existing_alb["cover_url"] = ia["cover_url"]
                    logger.info("[DISCOGRAPHY] Upgraded incomplete release '%s' with %d tracks from iTunes", ia["title"], len(i_tracks))
    except Exception as e:
        logger.warning("[DISCOGRAPHY] iTunes complementary sync error for '%s': %s", artist_name, e)

    # Sort albums chronologically (newest first or oldest first)
    filtered_albums.sort(key=lambda x: (x["year"] or 0, x["title"]), reverse=True)

    return {
        "artist_id": artist_id,
        "artist_name": artist_name,
        "artist_image": artist_image,
        "albums": filtered_albums,
    }


def _extract_genre(track_obj: dict) -> Optional[str]:
    """Best-effort primary genre name from a raw Deezer track object."""
    try:
        genres = (track_obj.get("genres") or {}).get("data") or []
        for g in genres:
            name = (g or {}).get("name", "")
            if name:
                return str(name)
    except Exception:
        pass
    return None


def get_track_info(track_id: int) -> dict:
    """Fetch single track metadata from Deezer."""
    data = _get(f"{DEEZER_API}/track/{track_id}")
    return {
        "id": data["id"],
        "title": data.get("title", ""),
        "artist_name": (data.get("artist") or {}).get("name", ""),
        "artist_id": (data.get("artist") or {}).get("id"),
        "album_title": (data.get("album") or {}).get("title", ""),
        "album_id": (data.get("album") or {}).get("id"),
        "duration": float(data.get("duration", 0)),
        "isrc": data.get("isrc"),
        "genre": _extract_genre(data),
        "release_date": data.get("release_date"),
    }


def get_album_info(album_id: int) -> dict:
    """Fetch single album metadata from Deezer."""
    data = _get(f"{DEEZER_API}/album/{album_id}")
    year_val = None
    rel_date = data.get("release_date") or ""
    if len(rel_date) >= 4 and rel_date[:4].isdigit():
        year_val = int(rel_date[:4])

    return {
        "id": data["id"],
        "title": data.get("title", ""),
        "artist_name": (data.get("artist") or {}).get("name", ""),
        "artist_id": (data.get("artist") or {}).get("id"),
        "year": year_val,
        "cover_url": data.get("cover_medium") or data.get("cover_big"),
        "record_type": (data.get("record_type") or "album").lower(),
    }


def get_album_tracks(album_id: int) -> list[dict]:
    """Fetch an album's track list from Deezer."""
    data = _get(f"{DEEZER_API}/album/{album_id}")
    out = []
    for t in (data.get("tracks") or {}).get("data", []):
        out.append({
            "id": t["id"],
            "title": t.get("title", ""),
            "track_position": t.get("track_position", 0),
            "disk_number": t.get("disk_number", 1),
            "duration": float(t.get("duration", 0) or 0),
        })
    return out


def search_album(query: str, limit: int = 20) -> list[dict]:
    """Search Deezer for albums by query."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        data = _get(f"{DEEZER_API}/search/album", {"q": q, "limit": limit})
        out = []
        for i in data.get("data", []):
            rel_date = i.get("release_date") or ""
            year_val = int(rel_date[:4]) if len(rel_date) >= 4 and rel_date[:4].isdigit() else None
            out.append({
                "id": i["id"],
                "title": i["title"],
                "artist_name": (i.get("artist") or {}).get("name", ""),
                "artist_id": (i.get("artist") or {}).get("id"),
                "cover_url": i.get("cover_medium"),
                "year": year_val,
            })
        return out
    except Exception as e:
        logger.warning("[DEEZER] Album search failed for '%s': %s", query, e)
        return []


def search_track(query: str, limit: int = 20) -> list[dict]:
    """Search Deezer for tracks by query."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        data = _get(f"{DEEZER_API}/search/track", {"q": q, "limit": limit})
        out = []
        for i in data.get("data", []):
            out.append({
                "id": i["id"],
                "title": i["title"],
                "artist_name": (i.get("artist") or {}).get("name", ""),
                "artist_id": (i.get("artist") or {}).get("id"),
                "album_title": (i.get("album") or {}).get("title", ""),
                "album_id": (i.get("album") or {}).get("id"),
                "duration": float(i.get("duration", 0)),
            })
        return out
    except Exception as e:
        logger.warning("[DEEZER] Track search failed for '%s': %s", query, e)
        return []

