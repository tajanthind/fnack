"""iTunes / Apple Music discography service: Free, zero-auth global music catalog to complement Deezer."""

import logging
import re
import time
import unicodedata
from typing import Any, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("fnack.itunes")

ITUNES_API = "https://itunes.apple.com"
TIMEOUT = 12

_session = requests.Session()
_retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
_adapter = HTTPAdapter(max_retries=_retries, pool_connections=10, pool_maxsize=20)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

_RE_REMIX = re.compile(
    r"\b(remix|remixes|remixed|club\s*mix|extended\s*mix|vip\s*mix|dub\s*mix|future\s*funk|chill\s*trap|amapiano|dance\s*version|jhankar\s*beats|edit)\b",
    re.IGNORECASE,
)
_RE_LOFI = re.compile(r"\b(lo-?fi|slowed|sped\s*up|speed\s*up|acoustic|instrumental|piano\s*version|orchestral)\b", re.IGNORECASE)
_RE_LIVE = re.compile(r"\b(live|in\s*concert|live\s*at|live\s*from|tour|unplugged)\b", re.IGNORECASE)
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


def _normalize(s: str) -> str:
    if not s:
        return ""
    # Strip (feat. ...), [feat. ...], (with ...), [with ...], (Deluxe...), etc.
    s = re.sub(r"[\(\[\{]\s*(?:feat\.?|featuring|with|deluxe|remastered|explicit|clean)\b[^\)\]\}]*[\)\]\}]", "", s, flags=re.IGNORECASE)
    # Strip trailing - Single, - EP, (Single), (EP), etc.
    s = re.sub(r"\s*-\s*(Single|EP|Album|Compilation|Soundtrack)$", "", s, flags=re.IGNORECASE)
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-zA-Z0-9]+", "", s).lower()


def _is_compilation_or_playlist(title: str, artist_name: str, collection_type: str = "") -> bool:
    """Detect if a release is a multi-artist DJ mix, playlist, or compilation."""
    t = title.lower()
    art = artist_name.lower()
    if "compilation" in collection_type.lower() or "various artists" in art:
        return True
    if _RE_PLAYLIST_COMPILATION.search(t) or _RE_PLAYLIST_COMPILATION.search(art):
        return True
    # If title contains "Mix" or "Collection" and is an album, it's a compilation / DJ mix
    if re.search(r"\b(mix|collection|hits|anthology)\b", t) and not any(k in t for k in ("soundtrack", "original motion picture")):
        return True
    tokens = re.split(r"[,;/+&]|\b(?:feat\.?|featuring|ft\.?|vs\.?|with|and|x|X)\b", artist_name, flags=re.IGNORECASE)
    clean_tokens = [tok.strip() for tok in tokens if tok.strip()]
    if len(clean_tokens) >= 4 and not any(k in t for k in ("soundtrack", "original motion picture", "from \"", "from '")):
        return True
    return False


def _should_filter_title(title: str, filter_remixes: bool, filter_lofi: bool, filter_live: bool) -> bool:
    t = title.lower()
    if filter_remixes and _RE_REMIX.search(t):
        return True
    if filter_lofi and _RE_LOFI.search(t):
        return True
    if filter_live and _RE_LIVE.search(t):
        return True
    return False


def _is_exact_artist_match(artist_query: str, release_artist_name: str) -> bool:
    """
    Check if artist_query is the exact primary artist or a discrete collaborator.
    Prevents loose substring matching (e.g. 'Dulla' will NEVER match 'Trevor Dulla', 'Abdullah', or 'Dulla Makabila').
    Supports all collaboration separators: commas, semicolons, &, +, /, feat, featuring, ft, vs, with, and, x, X.
    """
    if not artist_query or not release_artist_name:
        return False

    norm_target = _normalize(artist_query)
    if not norm_target:
        return False

    # Exact full match
    if _normalize(release_artist_name) == norm_target:
        return True

    # Split by standard collaboration separators
    collaborators = re.split(
        r"[,;/+&]|\b(?:feat\.?|featuring|ft\.?|vs\.?|with|and|x|X)\b",
        release_artist_name,
        flags=re.IGNORECASE,
    )
    for c in collaborators:
        if _normalize(c.strip()) == norm_target:
            return True

    return False


def get_itunes_album_tracks(collection_id: int) -> list[dict]:
    """Fetch tracklist for an iTunes collection."""
    try:
        url = f"{ITUNES_API}/lookup"
        resp = _session.get(url, params={"id": collection_id, "entity": "song"}, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if len(results) <= 1:
            return []

        tracks = []
        for item in results[1:]:
            if item.get("wrapperType") != "track":
                continue
            title = item.get("trackName") or ""
            duration_ms = item.get("trackTimeMillis") or 0
            tracks.append({
                "id": f"itunes_{item.get('trackId')}",
                "title": title,
                "duration": round(duration_ms / 1000.0, 1) if duration_ms else None,
                "track_position": item.get("trackNumber", len(tracks) + 1),
                "disk_number": item.get("discNumber", 1),
                "isrc": None,
            })
        return tracks
    except Exception as e:
        logger.warning("[ITUNES] Failed to fetch tracks for collection %d: %s", collection_id, e)
        return []


def get_itunes_artist_albums(
    artist_name: str,
    filter_remixes: bool = True,
    filter_lofi: bool = True,
    filter_live: bool = True,
    filter_compilations: bool = True,
    include_albums: bool = True,
    include_singles: bool = True,
    include_compilations: bool = False,
    existing_album_titles: Optional[set[str]] = None,
    limit: int = 200,
) -> list[dict]:
    """
    Search iTunes for artist releases and return structured album list with strict artist matching,
    artist ID disambiguation against existing known releases, and complete direct catalog lookup.
    """
    clean_artist = (artist_name or "").strip()
    if not clean_artist:
        return []

    try:
        url = f"{ITUNES_API}/search"
        resp = _session.get(url, params={"term": clean_artist, "entity": "album", "limit": limit}, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        search_results = data.get("results", [])

        # 1. Filter direct search matches by strict collaborator / primary artist equality
        matched_search_results = [
            a for a in search_results
            if a.get("collectionName") and a.get("collectionId") and _is_exact_artist_match(clean_artist, a.get("artistName", ""))
        ]

        # 2. Identify candidate primary artist profile IDs on iTunes (where profile name matches artist_name)
        norm_clean = _normalize(clean_artist)
        candidate_artist_ids: dict[Any, list[dict]] = {}
        for a in search_results:
            aid = a.get("artistId")
            art_name = a.get("artistName", "")
            if aid and _normalize(art_name) == norm_clean:
                candidate_artist_ids.setdefault(aid, []).append(a)

        # 3. Disambiguate primary artist profile IDs using known Deezer album overlap if available
        primary_artist_ids: set[Any] = set()
        rejected_artist_ids: set[Any] = set()

        if candidate_artist_ids:
            if existing_album_titles:
                norm_existing = {_normalize(t) for t in existing_album_titles if t}
                scores: dict[Any, int] = {}
                for aid, albs in candidate_artist_ids.items():
                    titles_norm = {_normalize(a.get("collectionName", "")) for a in albs}
                    scores[aid] = len(titles_norm.intersection(norm_existing))

                max_score = max(scores.values()) if scores else 0
                if max_score > 0:
                    for aid, sc in scores.items():
                        if sc == max_score or (max_score >= 2 and sc >= 1):
                            primary_artist_ids.add(aid)
                        else:
                            rejected_artist_ids.add(aid)
                    logger.info("[ITUNES] Disambiguated '%s' to verified iTunes artist ID(s) %s (rejected %s)", clean_artist, primary_artist_ids, rejected_artist_ids)
                else:
                    best_aid = max(candidate_artist_ids.keys(), key=lambda k: len(candidate_artist_ids[k]))
                    primary_artist_ids.add(best_aid)
                    for aid in candidate_artist_ids:
                        if aid != best_aid:
                            rejected_artist_ids.add(aid)
            else:
                best_aid = max(candidate_artist_ids.keys(), key=lambda k: len(candidate_artist_ids[k]))
                primary_artist_ids.add(best_aid)
                for aid in candidate_artist_ids:
                    if aid != best_aid:
                        rejected_artist_ids.add(aid)

        # 4. Perform direct catalog lookup for all verified primary artist IDs on iTunes
        all_raw_collections: dict[Any, dict] = {}
        for aid in primary_artist_ids:
            try:
                lookup_url = f"{ITUNES_API}/lookup"
                lookup_resp = _session.get(lookup_url, params={"id": aid, "entity": "album", "limit": limit}, timeout=TIMEOUT)
                lookup_resp.raise_for_status()
                lookup_data = lookup_resp.json()
                for item in lookup_data.get("results", []):
                    if item.get("wrapperType") == "collection" and item.get("collectionId"):
                        if _is_exact_artist_match(clean_artist, item.get("artistName", "")):
                            all_raw_collections[item["collectionId"]] = item
            except Exception as le:
                logger.warning("[ITUNES] Direct lookup failed for artist ID %s: %s", aid, le)

        # 5. Merge matched search results only if artistId is NOT from a rejected same-name profile
        for a in matched_search_results:
            cid = a.get("collectionId")
            aid = a.get("artistId")
            if cid and aid not in rejected_artist_ids and cid not in all_raw_collections:
                all_raw_collections[cid] = a

        if not all_raw_collections:
            return []

        albums = []
        seen_titles = set()

        for a in all_raw_collections.values():
            col_name = a.get("collectionName", "")
            art_name = a.get("artistName", "")
            col_id = a.get("collectionId")
            if not col_name or not col_id:
                continue

            norm_col = _normalize(col_name)
            if norm_col in seen_titles:
                continue
            seen_titles.add(norm_col)

            # Determine record type
            col_type = a.get("collectionType", "").lower()
            track_count = a.get("trackCount", 1)
            is_compilation = _is_compilation_or_playlist(col_name, art_name, col_type)
            is_single = ("- single" in col_name.lower() or track_count == 1) and not is_compilation
            is_ep = ("- ep" in col_name.lower() or (1 < track_count <= 6)) and not is_compilation

            if is_compilation and (filter_compilations or not include_compilations):
                continue
            if (is_single or is_ep) and not include_singles:
                continue
            if not (is_single or is_ep or is_compilation) and not include_albums:
                continue

            if _should_filter_title(col_name, filter_remixes, filter_lofi, filter_live):
                continue

            # Clean collection name (strip "- Single", "- EP")
            clean_name = re.sub(r"\s*-\s*(Single|EP)$", "", col_name, flags=re.IGNORECASE).strip()

            # Release year
            rel_date = a.get("releaseDate") or ""
            year_val = int(rel_date[:4]) if len(rel_date) >= 4 and rel_date[:4].isdigit() else None

            # High resolution artwork URL
            raw_art = a.get("artworkUrl100") or a.get("artworkUrl60") or ""
            cover_url = raw_art.replace("100x100bb.jpg", "600x600bb.jpg").replace("60x60bb.jpg", "600x600bb.jpg") if raw_art else None

            rec_type = "single" if is_single else ("ep" if is_ep else ("compile" if is_compilation else "album"))

            albums.append({
                "itunes_id": col_id,
                "title": clean_name,
                "raw_title": col_name,
                "record_type": rec_type,
                "cover_url": cover_url,
                "year": year_val,
                "track_count": track_count,
            })

        logger.info("[ITUNES] Discovered %d verified releases for '%s'", len(albums), clean_artist)
        return albums

    except Exception as e:
        logger.warning("[ITUNES] Failed to search albums for '%s': %s", clean_artist, e)
        return []

