"""Post-download audio verification service: Mutagen tag inspection & duration matching.

Guarantees the downloaded audio is the track it is supposed to be by checking:
  1. Duration delta against the official release (configurable tolerance / optional).
  2. Embedded artist + title tags against the expected track (confirmed mismatches
     are rejected; files without readable tags are only duration-checked).
"""

import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional, Tuple
import mutagen

logger = logging.getLogger("fnack.verifier")

# Sonarr-style matching strictness thresholds (maximum allowed duration delta in seconds)
STRICTNESS_DELTAS = {
    "strict": 4.0,    # Exact match (recommended for studio albums)
    "standard": 8.0,  # Standard tolerance (handles short music video intro/outro)
    "lenient": 15.0,  # Lenient tolerance
}
DEFAULT_DURATION_DELTA_SECONDS = 8.0

# Words that mark a file as a variant/wrong version when they appear in the file's
# own title but NOT in the expected track title (covers, live, karaoke, remixes, ...)
VARIANT_WORDS = {
    "cover", "live", "karaoke", "tribute", "instrumental", "acoustic", "slowed",
    "reverb", "remix", "radio edit", "single version", "album version", "demo",
    "mashup", "sped", "nightcore", "8d", "bass boost", "extended",
}


def _norm_text(s: str) -> str:
    """Normalize a string for containment matching (keeps word separators for segments)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", s)          # strip (…), […], {…}
    s = re.sub(r"\s*-\s*Topic\s*$", "", s, flags=re.IGNORECASE)  # "- Topic" suffix
    s = re.sub(
        r"\s*-\s*(?:official\s+)?(?:audio|video|lyrics?|music\s+video).*$",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"[^a-z0-9\s|-]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _primary_artist(s: str) -> str:
    """Extract the primary artist name (first part before feat. / & / , / x)."""
    if not s:
        return ""
    s = re.split(
        r"\s*(?:feat\.?|ft\.?|featuring|feat|&|\bwith\b|\band\b|,| x |\bx\b)\s*",
        str(s),
        flags=re.IGNORECASE,
    )[0]
    s = re.sub(r"\s*-\s*Topic\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", s)
    s = re.sub(r"[^a-zA-Z0-9]+", "", s)
    return s.lower()


def _extract_tags(mf) -> Tuple[Optional[str], Optional[str]]:
    """Pull (artist, title) tags from a mutagen file across formats."""
    artist = None
    title = None
    try:
        # dict-style formats (FLAC, Ogg, MP4, Opus, WAV ID3, ...)
        if hasattr(mf, "get"):
            for key in ("artist", "albumartist", "album artist", "\xa9ART", "aART", "TPE1", "TPE2"):
                v = mf.get(key)
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    v = v[0] if v else None
                if v:
                    artist = str(v)
                    break
            for key in ("title", "\xa9nam", "TIT2"):
                v = mf.get(key)
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    v = v[0] if v else None
                if v:
                    title = str(v)
                    break
        # ID3 frames on MP3 (mutagen.mp3.MP3 keeps tags dict-like as well)
        if (not artist or not title) and hasattr(mf, "tags") and mf.tags is not None:
            tags = mf.tags
            if not artist:
                for key in ("TPE1", "TPE2"):
                    frame = tags.get(key)
                    if frame:
                        try:
                            artist = str(frame)
                        except Exception:
                            pass
                        if artist:
                            break
            if not title:
                frame = tags.get("TIT2")
                if frame:
                    try:
                        title = str(frame)
                    except Exception:
                        pass
    except Exception as e:
        logger.debug("[VERIFIER] Tag extraction note: %s", e)
    return artist, title


def _artist_list(s: str) -> list:
    """All artist names in a multi-artist string, normalized."""
    if not s:
        return []
    parts = re.split(
        r"\s*(?:feat\.?|ft\.?|featuring|feat|&|\bwith\b|\band\b|,| x |\bx\b)\s*",
        str(s),
        flags=re.IGNORECASE,
    )
    out = []
    for p in parts:
        p = re.sub(r"\s*-\s*Topic\s*$", "", p, flags=re.IGNORECASE)
        p = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", p)
        norm = re.sub(r"[^a-zA-Z0-9]+", "", p)
        if norm:
            out.append(norm.lower())
    return out


def _check_artist(actual_artist: str, expected_artist: str) -> bool:
    """True when the expected artist appears among the file's artists (multi-artist aware).

    Falls back to fuzzy matching so close transliteration/spelling variants
    (e.g. 'Sharry Maan' vs 'Sherry Maan') are not falsely rejected.
    """
    from difflib import SequenceMatcher
    exp = _primary_artist(expected_artist)
    if not exp:
        return False
    acts = _artist_list(actual_artist)
    for a in acts:
        if a and (exp == a or exp in a or a in exp):
            return True
    for a in acts:
        if a and SequenceMatcher(None, exp, a).ratio() >= 0.8:
            return True
    return False


def _artist_in_text(text: str, expected_artist: str) -> bool:
    """Check whether the expected primary artist appears in a free-form title string.

    Uses a RAW normalization (parenthetical feat. clauses kept) because that is
    exactly where featured artists appear: 'Love Runs Out (feat. G-Eazy & Sasha
    Alex Sloan)' must confirm 'G-Eazy' even though the primary artist is
    'Martin Garrix'.
    """
    exp = _primary_artist(expected_artist)
    if not exp:
        return False
    raw = re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", str(text)).lower())
    if exp in raw:
        return True
    return exp in _norm_text(text).replace(" ", "")


def _check_title(actual_title: str, expected_title: str) -> bool:
    """Containment match, including against title segments ('A - B | C' style titles)."""
    act = _norm_text(actual_title)
    exp = _norm_text(expected_title)
    if not act or not exp:
        return False
    if exp in act:
        return True
    for seg in re.split(r"\s*[-|]\s*", act):
        if exp in seg:
            return True
    return False


def _check_variant(actual_title: str, expected_title: str) -> bool:
    """Reject variant/wrong versions (cover, live, karaoke, remix, ...)."""
    act = _norm_text(actual_title)
    exp = _norm_text(expected_title)
    if not act or not exp:
        return False
    for w in VARIANT_WORDS:
        norm_w = re.sub(r"[^a-z0-9]+", "", w)
        if not norm_w:
            continue
        pattern = re.escape(w.replace(" ", r"\s+"))
        if re.search(r"\b" + pattern + r"\b", act) and norm_w not in exp:
            return True
    return False


def verify_audio_file(
    file_path: Path,
    expected_duration_seconds: Optional[float] = None,
    expected_artist: Optional[str] = None,
    expected_title: Optional[str] = None,
    max_duration_delta: float = DEFAULT_DURATION_DELTA_SECONDS,
    delete_on_failure: bool = True,
    check_tags: bool = True,
) -> Tuple[bool, Optional[str], dict]:
    """
    Verify downloaded audio file validity and match against expected metadata.

    Enforces:
      - duration delta check when expected_duration_seconds is provided;
      - embedded tag (artist + title) check when expected_artist/expected_title are
        provided and the file carries readable tags. Confirmed mismatches are
        rejected; files without tags are only duration-checked.
    Returns (is_valid, error_reason, file_meta_dict).
    """
    if not file_path.is_file() or file_path.stat().st_size == 0:
        return False, "File does not exist or is empty", {}

    meta = {
        "file_path": str(file_path),
        "file_name": file_path.name,
        "file_format": file_path.suffix.lower().lstrip("."),
        "size_bytes": file_path.stat().st_size,
        "duration": None,
        "bitrate": None,
        "tag_artist": None,
        "tag_title": None,
    }

    try:
        mf = mutagen.File(str(file_path))
        if mf is None:
            if delete_on_failure:
                try:
                    file_path.unlink()
                except OSError:
                    pass
            return False, "Unrecognized or corrupted audio stream", meta

        if mf.info:
            meta["duration"] = getattr(mf.info, "length", None)
            meta["bitrate"] = getattr(mf.info, "bitrate", None)

        if check_tags and (expected_artist or expected_title):
            tag_artist, tag_title = _extract_tags(mf)
            meta["tag_artist"] = tag_artist
            meta["tag_title"] = tag_title

            # Tag verification: only reject on a CONFIRMED mismatch; missing tags
            # are unverifiable and pass through to the duration check.
            if expected_title and tag_title:
                if not _check_title(tag_title, expected_title):
                    err_msg = (
                        f"Tag mismatch: file title '{tag_title[:60]}' does not match "
                        f"expected '{expected_title[:60]}'"
                    )
                    logger.warning("[VERIFIER] %s for %s", err_msg, file_path.name)
                    if delete_on_failure:
                        try:
                            file_path.unlink()
                            logger.info("[VERIFIER] Deleted mismatched audio file: %s", file_path)
                        except OSError:
                            pass
                    return False, err_msg, meta
                if _check_variant(tag_title, expected_title):
                    err_msg = (
                        f"Tag mismatch: file title '{tag_title[:60]}' looks like a variant "
                        f"of '{expected_title[:60]}' (cover/live/karaoke/remix etc.)"
                    )
                    logger.warning("[VERIFIER] %s for %s", err_msg, file_path.name)
                    if delete_on_failure:
                        try:
                            file_path.unlink()
                            logger.info("[VERIFIER] Deleted mismatched audio file: %s", file_path)
                        except OSError:
                            pass
                    return False, err_msg, meta
            if expected_artist and tag_artist:
                artist_ok = _check_artist(tag_artist, expected_artist)
                # Fan/lyric channels often tag the channel as artist; fall back to
                # checking whether the expected artist appears in the file's title.
                if not artist_ok and tag_title:
                    artist_ok = _artist_in_text(tag_title, expected_artist)
                if not artist_ok:
                    err_msg = (
                        f"Tag mismatch: file artist '{tag_artist[:60]}' does not match "
                        f"expected '{expected_artist[:60]}'"
                    )
                    logger.warning("[VERIFIER] %s for %s", err_msg, file_path.name)
                    if delete_on_failure:
                        try:
                            file_path.unlink()
                            logger.info("[VERIFIER] Deleted mismatched audio file: %s", file_path)
                        except OSError:
                            pass
                    return False, err_msg, meta

    except Exception as e:
        logger.warning("[VERIFIER] Failed to read audio metadata for %s: %s", file_path.name, e)
        if delete_on_failure:
            try:
                file_path.unlink()
            except OSError:
                pass
        return False, f"Corrupted audio file: {e}", meta

    # Duration check if expected duration is provided
    actual_dur = meta.get("duration")
    if actual_dur and expected_duration_seconds and expected_duration_seconds > 0:
        delta = abs(actual_dur - expected_duration_seconds)
        if delta > max_duration_delta:
            err_msg = f"Duration mismatch: got {actual_dur:.1f}s, expected {expected_duration_seconds:.1f}s (delta {delta:.1f}s > {max_duration_delta:.1f}s tolerance)"
            logger.warning("[VERIFIER] %s for %s", err_msg, file_path.name)
            if delete_on_failure:
                try:
                    file_path.unlink()
                    logger.info("[VERIFIER] Deleted mismatched audio file: %s", file_path)
                except OSError:
                    pass
            return False, err_msg, meta

    logger.info(
        "[VERIFIER] Verified %s | Duration: %.1fs (expected: %s) | Artist: %s | Title: %s | Size: %d KB",
        file_path.name,
        actual_dur or 0,
        f"{expected_duration_seconds:.1f}s" if expected_duration_seconds else "N/A",
        (meta.get("tag_artist") or "N/A")[:30],
        (meta.get("tag_title") or "N/A")[:40],
        meta["size_bytes"] // 1024,
    )
    return True, None, meta
