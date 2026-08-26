"""yt-dlp fallback service: Intelligent audio extraction via YouTube & YouTube Music with cookies.txt support."""

import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple
import yt_dlp

from services.verifier_service import verify_audio_file

logger = logging.getLogger("fnack.ytdlp")

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"}
VARIANT_WORDS = {"cover", "live", "karaoke", "tribute", "instrumental", "acoustic", "slowed", "sped up", "lo-fi", "reverb", "teaser", "trailer", "short film", "skit", "reaction", "interview"}
VIDEO_EXTRANEOUS_WORDS = {"music video", "official video", "official music video", "musicvideo", "visualizer", "short film", "mv", "teaser"}

# Optional PO-token provider (bgutil-ytdlp-pot-provider sidecar). PO tokens are
# long-lived (months) and can bypass YouTube bot-checks without cookies. When
# POT_PROVIDER_URL is set, yt-dlp is told to fetch tokens from that provider.
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "").strip()


def _add_pot_provider_args(cmd: list) -> list:
    """Append --extractor-args for the PO token provider when configured."""
    if POT_PROVIDER_URL:
        cmd.extend([
            "--extractor-args",
            f"youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}",
        ])
    return cmd

# Possible cookies.txt search locations
DEFAULT_COOKIE_LOCATIONS = [
    Path(os.environ.get("CONFIG_DIR", "/config")) / "cookies.txt",
    Path("/config/cookies.txt"),
    Path(__file__).resolve().parent.parent / "config" / "cookies.txt",
]


def get_cookies_path(custom_path: Optional[str] = None) -> Optional[Path]:
    """Resolve active and valid cookies.txt file path."""
    if custom_path and str(custom_path).strip():
        cp = Path(custom_path.strip())
        if cp.exists() and cp.is_file() and cp.stat().st_size > 0:
            return cp

    for loc in DEFAULT_COOKIE_LOCATIONS:
        try:
            if loc.exists() and loc.is_file() and loc.stat().st_size > 0:
                return loc
        except OSError:
            pass
    return None


def _copy_cookies_for_ytdlp(cookies_path: Optional[str], dest_dir: Path) -> Optional[str]:
    """Copy the user's cookies file so yt-dlp can read AND dump its cookie jar
    without overwriting the original (yt-dlp rewrites --cookies files on exit).
    Returns the copy path, or None when no valid cookies file exists."""
    cp = get_cookies_path(cookies_path)
    if not cp:
        return None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ".fnack_cookies.txt"
        shutil.copy2(str(cp), str(dest))
        return str(dest)
    except OSError as e:
        logger.debug("[YT-DLP] Cookies copy note: %s", e)
        return str(cp)  # fall back to the original path


def get_cookies_status(custom_path: Optional[str] = None) -> dict:
    """Return status and details of cookies.txt file for UI and settings verification."""
    cp = get_cookies_path(custom_path)
    if not cp or not cp.exists():
        return {
            "configured": False,
            "path": str(DEFAULT_COOKIE_LOCATIONS[0]),
            "exists": False,
            "size_bytes": 0,
            "cookie_count": 0,
            "last_modified": None,
            "message": "No cookies.txt file found. YouTube may restrict some streams without login.",
        }

    try:
        content = cp.read_text(encoding="utf-8", errors="ignore")
        lines = [l.strip() for l in content.splitlines() if l.strip() and not l.startswith("#")]
        stat = cp.stat()
        return {
            "configured": True,
            "path": str(cp),
            "exists": True,
            "size_bytes": stat.st_size,
            "cookie_count": len(lines),
            "last_modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "message": f"Active ({len(lines)} cookies loaded from {cp.name}). This file lives on your host volume (e.g. ./config/cookies.txt) — it is NOT baked into the Docker image.",
        }
    except Exception as e:
        return {
            "configured": False,
            "path": str(cp),
            "exists": True,
            "size_bytes": 0,
            "cookie_count": 0,
            "last_modified": None,
            "message": f"Error reading cookies.txt: {e}",
        }


def _normalize_str(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", s)
    return re.sub(r"[^a-zA-Z0-9]+", "", s).lower()


def find_youtube_candidates(
    artist_name: str,
    track_title: str,
    expected_duration: Optional[float] = None,
    max_duration_delta: float = 12.0,
    prefer_youtube_music: bool = True,
    cookies_path: Optional[str] = None,
) -> list[dict]:
    """
    Search YouTube Music and YouTube for audio candidates and score them.
    Prioritizes official YouTube Music Topic tracks and clean audio streams over music videos.
    Returns sorted list of candidate dictionaries in descending score order.
    """
    clean_art = (artist_name or "").strip()
    clean_tit = (track_title or "").strip()
    norm_art = _normalize_str(clean_art)
    norm_tit = _normalize_str(clean_tit)

    # Queries ordered to prioritize official YouTube Music / Topic releases before general video search
    queries = [
        f'"{clean_art} - Topic" "{clean_tit}"',
        f'"{clean_art}" "{clean_tit}" official audio',
        f'{clean_art} - {clean_tit} Audio',
        f'{clean_art} - {clean_tit}',
    ]

    resolved_cookie_file = get_cookies_path(cookies_path)

    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "no_warnings": True,
    }
    if resolved_cookie_file:
        ydl_opts["cookiefile"] = str(resolved_cookie_file)

    if Path("/usr/bin/node").exists():
        ydl_opts["js_runtimes"] = {"node": {"path": "/usr/bin/node"}}
    elif Path("/usr/local/bin/deno").exists():
        ydl_opts["js_runtimes"] = {"deno": {"path": "/usr/local/bin/deno"}}

    seen_ids = set()
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
                    if cand_id in seen_ids:
                        continue
                    seen_ids.add(cand_id)

                    cand_title = item.get("title", "")
                    cand_uploader = item.get("uploader", "")
                    cand_dur = float(item.get("duration") or 0)
                    norm_cand_tit = _normalize_str(cand_title)
                    norm_cand_up = _normalize_str(cand_uploader)

                    # Score candidate
                    score = 0

                    # Topic channel match (Official YouTube Music release)
                    if "topic" in cand_uploader.lower() and norm_art in norm_cand_up:
                        score += 15
                    elif "official" in cand_uploader.lower() or "vevo" in cand_uploader.lower():
                        score += 5

                    # Title match
                    if norm_tit in norm_cand_tit:
                        score += 8
                    elif any(word.lower() in cand_title.lower() for word in clean_tit.split() if len(word) > 3):
                        score += 3
                    else:
                        score -= 8

                    # Artist match in title or uploader
                    if norm_art in norm_cand_tit or norm_art in norm_cand_up:
                        score += 5
                    else:
                        score -= 3

                    # Clean audio vs Music Video penalty
                    for vid_word in VIDEO_EXTRANEOUS_WORDS:
                        if vid_word in cand_title.lower() and vid_word not in clean_tit.lower():
                            score -= 6

                    if "audio" in cand_title.lower() and "video" not in cand_title.lower():
                        score += 4

                    # Duration scoring
                    if expected_duration and expected_duration > 0 and cand_dur > 0:
                        dur_delta = abs(cand_dur - expected_duration)
                        if dur_delta <= 2.5:
                            score += 12
                        elif dur_delta <= 5.0:
                            score += 7
                        elif dur_delta <= max_duration_delta:
                            score += 2
                        else:
                            score -= 15

                    # Variant penalty
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

        except Exception as e:
            logger.debug("[YT-DLP] Search candidate query '%s' failed: %s", q, e)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return [c for c in candidates if c["score"] >= 0]


def find_best_youtube_candidate(
    artist_name: str,
    track_title: str,
    expected_duration: Optional[float] = None,
    max_duration_delta: float = 12.0,
    prefer_youtube_music: bool = True,
    cookies_path: Optional[str] = None,
) -> Optional[dict]:
    """Find the single highest-scoring YouTube candidate."""
    candidates = find_youtube_candidates(
        artist_name=artist_name,
        track_title=track_title,
        expected_duration=expected_duration,
        max_duration_delta=max_duration_delta,
        prefer_youtube_music=prefer_youtube_music,
        cookies_path=cookies_path,
    )
    return candidates[0] if candidates else None


def _extract_ytdlp_error(output: str, max_chars: int = 700) -> str:
    """Pull the meaningful failure text out of yt-dlp's raw output.

    Skips download-progress noise (MiB/s counters, 'Destination:', playlist
    item lines) and keeps real error/warning lines, or the last meaningful
    line when no explicit error was printed (e.g. 'Finished downloading
    playlist: X' for a 0-item SoundCloud album result).
    """
    if not output or not output.strip():
        return "No output produced"

    noise_fragments = (
        "MiB/s", "KiB/s", "Destination:", "[download]", "[ExtractAudio]",
        "Deleting original", "Extracting URL", "Downloading page",
        "already been downloaded", "has already been", "ETA ",
    )
    noise_line_start = (
        "[download]", "[Merger]", "[ExtractAudio]", "[Metadata]", "[Fixup]",
        "[ffmpeg]", "[info]", "[debug]", "[youtube]", "[youtubetab]",
        "[generic]", "[soundcloud]", "[SponsorBlock]", "[MoveFiles]",
        "[EmbedThumbnail]", "[VideoConvertor]", "[ModifyChapters]",
        "[niconico]", "[DASH]", "[youtube:playlist]", "[download] has already",
    )
    progress_re = re.compile(r"^\s*\d+(\.\d+)?\s*%|^\s*\[download\]|^\s*100%| in 00:0\d|^\s*0:0\d")

    interesting = []
    playlist_hint = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        low = line.lower()
        if any(n in low for n in noise_fragments):
            continue
        if line.startswith(noise_line_start):
            continue
        if progress_re.match(line) or "% of" in low:
            continue
        if "playlist" in low:
            playlist_hint = line
            continue
        if any(k in low for k in (
            "error:", "warning:", "http error", "sign in", "unable",
            "not available", "not a valid url", "requested format",
            "did not get any data", "failed", "403", "404", "410",
            "geo-restricted", "drm", "is not supported", "unsupported",
            "no video", "no result", "nothing found", "cannot", "unable to",
            "requested format is not available",
        )):
            interesting.append(line)
    if interesting:
        snippet = " | ".join(interesting[-4:])
    elif playlist_hint:
        snippet = f"Search returned a playlist/album, not a track: {playlist_hint}"
    else:
        # No explicit error: pick the last line that is not pure progress/noise.
        # Prefer lines with alphabetic content; ignore stray counters/timestamps
        # and truncated filename fragments ("0:00", "ck.m4a\"") that yt-dlp
        # leaves behind on failed runs.
        candidates = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            low = line.lower()
            if any(n in low for n in noise_fragments):
                continue
            if line.startswith(noise_line_start):
                continue
            if progress_re.match(line) or "% of" in low:
                continue
            if not re.search(r"[a-z]", low):
                continue  # pure numbers/timestamps — not useful
            if len(line) > 2:
                candidates.append(line)
        # Drop lines that are clearly truncated path/name fragments
        filtered = [
            ln for ln in candidates
            if not (ln.endswith(('"', "\\")) or (len(ln) < 12 and " " not in ln))
        ]
        if filtered:
            snippet = filtered[-1]
        else:
            snippet = "No detailed error available (see container logs)"
    return snippet[:max_chars]


def download_track_ytdlp(
    query_or_url: str,
    output_dir: Path,
    output_format: str = "flac",
    artist_name: Optional[str] = None,
    track_title: Optional[str] = None,
    expected_duration: Optional[float] = None,
    cookies_path: Optional[str] = None,
    prefer_youtube_music: bool = True,
    timeout_seconds: int = 180,
    max_duration_delta: float = 8.0,
    check_duration: bool = True,
) -> Tuple[bool, Optional[Path], Optional[str]]:
    """
    Download a single audio track using yt-dlp with candidate scoring, YouTube Music prioritization,
    automatic multi-candidate fallback, and cookies.txt authentication.
    Each candidate's duration is verified against the expected duration before it is accepted,
    so a wrong first search result is discarded and the next candidate is tried.
    Returns (success, output_file_path, error_message).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    target = query_or_url.strip()

    # IMPORTANT: pass yt-dlp a COPY of the cookies file, never the original.
    # yt-dlp dumps its cookie jar back into --cookies files on exit, which would
    # overwrite the user's uploaded cookies.txt (the 'reverts to 13 cookies' bug).
    cookie_file_to_use = _copy_cookies_for_ytdlp(cookies_path, output_dir)

    # Targets to try in order of priority (YouTube Music candidates first, then fallbacks)
    targets_to_try = []

    if artist_name and track_title and not (target.startswith("http://") or target.startswith("https://")):
        candidates = find_youtube_candidates(
            artist_name=artist_name,
            track_title=track_title,
            expected_duration=expected_duration,
            prefer_youtube_music=prefer_youtube_music,
            cookies_path=cookie_file_to_use,
        )
        if candidates:
            targets_to_try = [c["url"] for c in candidates]
        else:
            targets_to_try = [f"ytsearch1:{target}"]

        # Try BOTH the regular YouTube and YouTube Music frontends for every candidate
        # (music.youtube.com sometimes bypasses bot-checks that block www.youtube.com)
        expanded = []
        for t in targets_to_try:
            m = re.search(r"(?:youtube\.com|youtu\.be)/watch\?v=([a-zA-Z0-9_-]{6,})", t)
            if m:
                vid = m.group(1)
                expanded.append(f"https://www.youtube.com/watch?v={vid}")
                expanded.append(f"https://music.youtube.com/watch?v={vid}")
            else:
                expanded.append(t)
        targets_to_try = expanded

        # Add SoundCloud fallback targets for resilient zero-account audio downloading
        clean_t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", track_title).strip()
        targets_to_try.append(f'scsearch2:"{artist_name}" "{clean_t}"')
        targets_to_try.append(f"scsearch2:{artist_name} {clean_t}")
    elif not (target.startswith("http://") or target.startswith("https://") or target.startswith("ytsearch") or target.startswith("scsearch")):
        targets_to_try = [f"ytsearch1:{target}", f"scsearch1:{target}"]
    else:
        targets_to_try = [target]

    last_error = ""
    audio_files = []  # populated per-target; initialized here for safety

    def _accept_or_reject(cand_target: str, produced: Path) -> bool:
        """Verify a produced audio file against the expected track; delete on mismatch."""
        # Duration check (only when the feature is enabled)
        if expected_duration and expected_duration > 0 and check_duration:
            v_ok, v_err, _meta = verify_audio_file(
                produced,
                expected_duration_seconds=expected_duration,
                expected_artist=artist_name,
                expected_title=track_title,
                max_duration_delta=max_duration_delta,
                delete_on_failure=True,
            )
            if not v_ok:
                logger.info("[YT-DLP] Candidate '%s' rejected (%s); trying next candidate...", cand_target, v_err)
                return False
        else:
            # Duration check disabled: still reject a CONFIRMED wrong song via tags
            v_ok, v_err, _meta = verify_audio_file(
                produced,
                expected_duration_seconds=None,
                expected_artist=artist_name,
                expected_title=track_title,
                delete_on_failure=True,
            )
            if not v_ok:
                logger.info("[YT-DLP] Candidate '%s' rejected (%s); trying next candidate...", cand_target, v_err)
                return False
        logger.info("[YT-DLP] Successfully downloaded: %s (%d bytes)", produced.name, produced.stat().st_size)
        return True

    for cand_target in targets_to_try:
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "-f",
            "ba/ba*/bestaudio/best",
            "-x",
            "--audio-format",
            output_format,
            "--audio-quality",
            "0",
            "-o",
            str(output_dir / "%(title)s.%(ext)s"),
            "--no-warnings",
        ]
        _add_pot_provider_args(cmd)

        if cookie_file_to_use:
            cmd.extend(["--cookies", cookie_file_to_use])

        if Path("/usr/bin/node").exists():
            cmd.extend(["--js-runtimes", "node:/usr/bin/node"])
        elif Path("/usr/local/bin/deno").exists():
            cmd.extend(["--js-runtimes", "deno:/usr/local/bin/deno"])

        cmd.append(cand_target)

        logger.info("[YT-DLP] Executing target: %s", cand_target)

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
                logger.warning("[YT-DLP] Process timed out after %ds for %s", timeout_seconds, cand_target)
                last_error = f"yt-dlp timed out after {timeout_seconds}s"
                continue

            full_output = "\n".join(stdout_lines).strip()

            # Find produced audio file in output_dir
            audio_files = [f for f in output_dir.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]

            if audio_files:
                latest_file = max(audio_files, key=lambda f: f.stat().st_mtime)
                if _accept_or_reject(cand_target, latest_file):
                    return True, latest_file, None

            # Clean error snippet
            err_snippet = _extract_ytdlp_error(full_output)
            last_error = f"yt-dlp error: {err_snippet}"
            logger.debug("[YT-DLP] Candidate '%s' failed (%s), trying next candidate if available...", cand_target, err_snippet)

        except Exception as e:
            logger.exception("[YT-DLP] Execution error for %s: %s", cand_target, e)
            last_error = str(e)

    # Zero-auth resilience: if YouTube refused every candidate with a bot/sign-in
    # check (or stale cookies return 'Video unavailable'), retry the same targets
    # once using the Android player client, which frequently bypasses the anti-bot
    # gate. Runs even when a cookie file exists (a stale/expired cookie file must
    # not block this fallback).
    if not audio_files and last_error and any(
        w in last_error.lower() for w in (
            "sign in to confirm", "not a bot", "bot check", "login required",
            "confirm you", "video unavailable",
        )
    ):
        logger.info("[YT-DLP] YouTube bot-check detected and no cookies configured; retrying with Android player client...")
        for cand_target in targets_to_try:
            if "youtube.com" not in cand_target and "youtu.be" not in cand_target:
                continue
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--no-playlist",
                "-f",
                "ba/ba*/bestaudio/best",
                "-x",
                "--audio-format",
                output_format,
                "--audio-quality",
                "0",
                "-o",
                str(output_dir / "%(title)s.%(ext)s"),
                "--no-warnings",
                "--extractor-args",
                "youtube:player_client=android",
            ]
            _add_pot_provider_args(cmd)
            if cookie_file_to_use:
                cmd.extend(["--cookies", cookie_file_to_use])
            if Path("/usr/bin/node").exists():
                cmd.extend(["--js-runtimes", "node:/usr/bin/node"])
            elif Path("/usr/local/bin/deno").exists():
                cmd.extend(["--js-runtimes", "deno:/usr/local/bin/deno"])
            cmd.append(cand_target)

            logger.info("[YT-DLP] (android client) Executing target: %s", cand_target)
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )
                try:
                    out, _ = proc.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    last_error = f"yt-dlp timed out after {timeout_seconds}s"
                    continue

                audio_files = [f for f in output_dir.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]
                if audio_files:
                    latest_file = max(audio_files, key=lambda f: f.stat().st_mtime)
                    if _accept_or_reject(cand_target, latest_file):
                        return True, latest_file, None
            except Exception as e:
                logger.exception("[YT-DLP] Android-client execution error for %s: %s", cand_target, e)
                last_error = str(e)

    logger.warning("[YT-DLP] All candidates failed for '%s'. Last error: %s", query_or_url, last_error)

    # User-actionable hint: stale/expired YouTube cookies produce exactly this
    # pattern (search works, but stream downloads fail with 'Video unavailable'
    # or bot-checks). Tell the user to re-export cookies once.
    if last_error and any(w in last_error.lower() for w in ("video unavailable", "sign in to confirm", "not a bot", "bot check")):
        if get_cookies_path(cookies_path):
            logger.warning(
                "[YT-DLP] Hint: these YouTube failures often mean the cookies.txt file is stale/expired. "
                "Re-export fresh cookies from a signed-in browser (Settings -> YouTube Cookies) and retry."
            )
        else:
            logger.warning(
                "[YT-DLP] Hint: YouTube is bot-blocking this IP without cookies. Add cookies.txt "
                "(Settings -> YouTube Cookies) and retry."
            )
    return False, None, last_error



