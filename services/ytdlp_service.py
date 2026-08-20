"""yt-dlp fallback service: Intelligent audio extraction via YouTube & YouTube Music."""

import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Optional, Tuple
import yt_dlp

logger = logging.getLogger("fnack.ytdlp")

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"}
VARIANT_WORDS = {"cover", "live", "karaoke", "tribute", "instrumental", "acoustic", "slowed", "sped up", "lo-fi", "reverb"}


def _normalize_str(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", s)
    return re.sub(r"[^a-zA-Z0-9]+", "", s).lower()


def find_best_youtube_candidate(
    artist_name: str,
    track_title: str,
    expected_duration: Optional[float] = None,
    max_duration_delta: float = 12.0,
) -> Optional[dict]:
    """
    Search YouTube Music and YouTube for candidates and score them to find the exact audio match.
    Prioritizes official label Topic channels and exact duration matches.
    """
    clean_art = (artist_name or "").strip()
    clean_tit = (track_title or "").strip()
    norm_art = _normalize_str(clean_art)
    norm_tit = _normalize_str(clean_tit)

    queries = [
        f'"{clean_art} - Topic" "{clean_tit}"',
        f'"{clean_art}" "{clean_tit}" official audio',
        f'{clean_art} - {clean_tit} Audio',
        f'{clean_art} - {clean_tit}',
    ]

    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "no_warnings": True,
    }

    candidates = []

    for q in queries:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(f"ytsearch5:{q}", download=False)
                entries = res.get("entries", []) if res else []
                for item in entries:
                    if not item or not item.get("id"):
                        continue

                    cand_id = item.get("id")
                    cand_title = item.get("title", "")
                    cand_uploader = item.get("uploader", "")
                    cand_dur = float(item.get("duration") or 0)
                    norm_cand_tit = _normalize_str(cand_title)
                    norm_cand_up = _normalize_str(cand_uploader)

                    # Score candidate
                    score = 0

                    # Topic channel match (Official YouTube Music Audio)
                    if "topic" in cand_uploader.lower() and norm_art in norm_cand_up:
                        score += 10
                    elif "official" in cand_uploader.lower() or "vevo" in cand_uploader.lower():
                        score += 4

                    # Title match
                    if norm_tit in norm_cand_tit:
                        score += 8
                    elif any(word.lower() in cand_title.lower() for word in clean_tit.split() if len(word) > 3):
                        score += 3
                    else:
                        # Video title doesn't even contain the track title words
                        score -= 6

                    # Artist match in title or uploader
                    if norm_art in norm_cand_tit or norm_art in norm_cand_up:
                        score += 5
                    else:
                        score -= 3

                    # Duration scoring
                    if expected_duration and expected_duration > 0 and cand_dur > 0:
                        dur_delta = abs(cand_dur - expected_duration)
                        if dur_delta <= 3.0:
                            score += 10
                        elif dur_delta <= 6.0:
                            score += 6
                        elif dur_delta <= max_duration_delta:
                            score += 2
                        else:
                            # Heavy penalty for big duration discrepancy
                            score -= 15

                    # Variant penalty if not in expected title
                    for v in VARIANT_WORDS:
                        if v in cand_title.lower() and v not in clean_tit.lower():
                            score -= 8

                    candidates.append({
                        "score": score,
                        "id": cand_id,
                        "url": f"https://www.youtube.com/watch?v={cand_id}",
                        "title": cand_title,
                        "uploader": cand_uploader,
                        "duration": cand_dur,
                    })

            if candidates and any(c["score"] >= 20 for c in candidates):
                break  # Found high-confidence candidate early
        except Exception as e:
            logger.debug("[YT-DLP] Search candidate query '%s' failed: %s", q, e)

    if not candidates:
        return None

    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0]

    if best["score"] < 5:
        logger.warning(
            "[YT-DLP] No confident YouTube match for '%s - %s' (best score %d: '%s' by '%s')",
            clean_art, clean_tit, best["score"], best["title"], best["uploader"]
        )
        return None

    logger.info(
        "[YT-DLP] Selected best match for '%s - %s': '%s' [%.0fs] by '%s' (score %d, id %s)",
        clean_art, clean_tit, best["title"], best["duration"], best["uploader"], best["score"], best["id"]
    )
    return best


def download_track_ytdlp(
    query_or_url: str,
    output_dir: Path,
    output_format: str = "flac",
    artist_name: Optional[str] = None,
    track_title: Optional[str] = None,
    expected_duration: Optional[float] = None,
    timeout_seconds: int = 180,
) -> Tuple[bool, Optional[Path], Optional[str]]:
    """
    Download a single audio track using yt-dlp with candidate scoring.
    Returns (success, output_file_path, error_message).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    target = query_or_url.strip()

    # If artist and title are provided, find the verified best YouTube match
    if artist_name and track_title and not (target.startswith("http://") or target.startswith("https://")):
        candidate = find_best_youtube_candidate(artist_name, track_title, expected_duration)
        if candidate:
            target = candidate["url"]
        else:
            if not target.startswith("ytsearch"):
                target = f"ytsearch1:{target}"
    elif not (target.startswith("http://") or target.startswith("https://") or target.startswith("ytsearch")):
        target = f"ytsearch1:{target}"

    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--extractor-args",
        "youtube:player_client=android,ios,web",
        "-x",
        "--audio-format",
        output_format,
        "--audio-quality",
        "0",
        "-o",
        str(output_dir / "%(title)s.%(ext)s"),
        "--no-warnings",
        target,
    ]

    logger.info("[YT-DLP] Executing: %s", target)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )

        stdout_lines = []
        try:
            out, _ = proc.communicate(timeout=timeout_seconds)
            stdout_lines.append(out or "")
        except subprocess.TimeoutExpired:
            proc.kill()
            logger.warning("[YT-DLP] Process timed out after %ds for %s", timeout_seconds, target)
            return False, None, f"yt-dlp timed out after {timeout_seconds}s"

        full_output = "\n".join(stdout_lines).strip()

        # Find produced audio file in output_dir
        audio_files = [f for f in output_dir.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]

        if proc.returncode == 0 and audio_files:
            latest_file = max(audio_files, key=lambda f: f.stat().st_mtime)
            logger.info("[YT-DLP] Successfully downloaded: %s (%d bytes)", latest_file.name, latest_file.stat().st_size)
            return True, latest_file, None

        if audio_files:
            latest_file = max(audio_files, key=lambda f: f.stat().st_mtime)
            logger.info("[YT-DLP] Audio file found despite non-zero code: %s", latest_file.name)
            return True, latest_file, None

        # Clean error snippet
        err_lines = [l for l in full_output.splitlines() if "ERROR:" in l or "WARNING:" in l or "HTTP Error" in l]
        err_snippet = "\n".join(err_lines[-3:]) if err_lines else (full_output[-300:] if full_output else "No output produced")
        logger.warning("[YT-DLP] Download failed for '%s': %s", target, err_snippet)
        return False, None, f"yt-dlp error: {err_snippet}"

    except Exception as e:
        logger.exception("[YT-DLP] Execution error for %s: %s", target, e)
        return False, None, str(e)

