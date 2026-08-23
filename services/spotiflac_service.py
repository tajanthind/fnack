"""SpotiFLAC CLI service: Downloads true lossless FLAC without authentication via Tidal/Qobuz/SoundCloud/Deezer."""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("fnack.spotiflac")

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"}
DEFAULT_REGISTRY_URL = "https://raw.githubusercontent.com/spotiflacapp/SpotiFLAC-Extension/refs/heads/main/registry.json"

_initialized = False
_init_lock = threading.Lock()

# Thread-safe rate limiter and concurrency lock
_spotiflac_lock = threading.Lock()
_last_spotiflac_time = 0.0
_DEFAULT_DELAY = 1.5  # Seconds between SpotiFLAC process invocations


def set_spotiflac_pacing_delay(seconds: float) -> None:
    """Configure the pacing delay between consecutive SpotiFLAC downloads."""
    global _DEFAULT_DELAY
    _DEFAULT_DELAY = max(0.5, float(seconds))


def _pace_spotiflac_call(delay: Optional[float] = None) -> None:
    """Thread-safe rate limiter to avoid 429 rate limits from upstream lossless providers."""
    global _last_spotiflac_time
    wait_time = delay if delay is not None else _DEFAULT_DELAY
    now = time.time()
    elapsed = now - _last_spotiflac_time
    if elapsed < wait_time:
        sleep_amount = wait_time - elapsed
        logger.debug("[SPOTIFLAC] Rate limiter pacing: sleeping %.2fs", sleep_amount)
        time.sleep(sleep_amount)
    _last_spotiflac_time = time.time()


def ensure_xvfb() -> None:
    """Ensure Xvfb virtual framebuffer display is active on :99 for headless browser solving."""
    try:
        res = subprocess.run(["pgrep", "-f", "Xvfb :99"], capture_output=True)
        if res.returncode != 0:
            # Remove stale lock/socket files that survive container restarts and block Xvfb startup
            for stale in ("/tmp/.X99-lock", "/tmp/.X11-unix/X99"):
                try:
                    os.remove(stale)
                except OSError:
                    pass
            logger.info("[SPOTIFLAC] Starting background Xvfb on display :99...")
            subprocess.Popen(
                ["Xvfb", ":99", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
    except Exception as e:
        logger.debug("[SPOTIFLAC] Xvfb ensure note: %s", e)


def ensure_spotiflac_extensions() -> None:
    """Ensure SpotiFLAC extension registry and zero-auth providers are installed and active."""
    global _initialized
    with _init_lock:
        ensure_xvfb()
        if _initialized:
            return
        try:
            os.environ["SPOTIFLAC_REGISTRIES"] = DEFAULT_REGISTRY_URL
            from SpotiFLAC.extensions.manager import ExtensionManager
            from SpotiFLAC.extensions.registry_config import add_registry

            add_registry(DEFAULT_REGISTRY_URL)
            mgr = ExtensionManager()
            installed = mgr.list_installed()
            if len(installed) < 3:
                logger.info("[SPOTIFLAC] Fetching and installing lossless extension providers...")
                entries = mgr.fetch_registry(DEFAULT_REGISTRY_URL)
                for e in entries:
                    try:
                        ext_id = getattr(e, "name", None) or getattr(e, "id", None) or str(e)
                        mgr.install(ext_id)
                    except Exception as ie:
                        logger.debug("[SPOTIFLAC] Extension install %s: %s", getattr(e, "name", e), ie)
                installed = mgr.list_installed()
                logger.info("[SPOTIFLAC] Active lossless providers: %s", [x.name for x in installed])
            _initialized = True
        except Exception as e:
            logger.warning("[SPOTIFLAC] Extension auto-init note: %s", e)


def download_track_spotiflac(
    spotify_url: str,
    output_dir: Path,
    quality: str = "LOSSLESS",
    services: Optional[list[str]] = None,
    timeout_seconds: int = 180,
    rate_limit_delay: Optional[float] = None,
    max_retries: int = 2,
) -> Tuple[bool, Optional[Path], Optional[str]]:
    """
    Download a single track using SpotiFLAC (zero-auth lossless FLAC).
    Thread-safe rate-limited execution with automatic backoff retry.
    Returns (success, output_file_path, error_message).
    """
    ensure_spotiflac_extensions()
    output_dir.mkdir(parents=True, exist_ok=True)

    active_services = services if (services and len(services) > 0) else [
        "ext:tidal-web",
        "ext:qobuz-web",
        "ext:deezer",
        "ext:soundcloud",
        "ext:ytmusic-spotiflac",
    ]

    cmd = [
        "spotiflac",
        spotify_url,
        str(output_dir),
        "--quality",
        quality,
        "--service",
        *active_services,
        "--retries",
        "1",
        "--filename-format",
        "{track}. {title}",
    ]

    proc_env = {
        **os.environ,
        "SPOTIFLAC_REGISTRIES": DEFAULT_REGISTRY_URL,
        "CHROME_PATH": os.environ.get("CHROME_PATH", "/usr/bin/chromium"),
        "DISPLAY": os.environ.get("DISPLAY", ":99"),
    }

    last_error = ""

    for attempt in range(1, max_retries + 1):
        # Serialize SpotiFLAC process executions with rate limiting lock and pacing delay
        with _spotiflac_lock:
            _pace_spotiflac_call(rate_limit_delay)
            logger.info("[SPOTIFLAC] (Attempt %d/%d) Running: %s", attempt, max_retries, " ".join(cmd))

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    env=proc_env,
                )

                stdout_lines = []
                try:
                    out, _ = proc.communicate(timeout=timeout_seconds)
                    stdout_lines.append(out or "")
                except subprocess.TimeoutExpired:
                    proc.kill()
                    logger.warning("[SPOTIFLAC] Process timed out after %ds for %s", timeout_seconds, spotify_url)
                    last_error = f"SpotiFLAC timed out after {timeout_seconds}s"
                    continue

                full_output = "\n".join(stdout_lines)

                # Find produced audio file in output_dir
                audio_files = [f for f in output_dir.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]

                if proc.returncode == 0 and audio_files:
                    latest_file = max(audio_files, key=lambda f: f.stat().st_mtime)
                    logger.info("[SPOTIFLAC] Successfully downloaded: %s (%d bytes)", latest_file.name, latest_file.stat().st_size)
                    return True, latest_file, None

                if audio_files:
                    latest_file = max(audio_files, key=lambda f: f.stat().st_mtime)
                    logger.info("[SPOTIFLAC] Output file found despite non-zero exit: %s", latest_file.name)
                    return True, latest_file, None

                err_snippet = full_output[-500:].strip() if full_output else "No output"
                last_error = f"SpotiFLAC produced no audio file: {err_snippet}"

                # Check if rate limit / 429 error occurred in output
                if any(w in full_output.lower() for w in ("429", "rate limit", "too many requests", "throttle")):
                    logger.warning("[SPOTIFLAC] Rate limit detected from upstream provider on attempt %d. Backing off...", attempt)
                    time.sleep(3.0 * attempt)

            except Exception as e:
                logger.exception("[SPOTIFLAC] Execution error for %s: %s", spotify_url, e)
                last_error = str(e)

        if attempt < max_retries:
            time.sleep(2.0 * attempt)

    logger.warning("[SPOTIFLAC] All %d attempts failed for %s. Last error: %s", max_retries, spotify_url, last_error)
    return False, None, last_error


def download_album_spotiflac(
    spotify_album_url: str,
    output_dir: Path,
    quality: str = "LOSSLESS",
    services: Optional[list[str]] = None,
    timeout_seconds: int = 600,
    rate_limit_delay: Optional[float] = None,
    max_retries: int = 2,
) -> Tuple[bool, list, Optional[str]]:
    """
    Download a full album using SpotiFLAC (zero-auth lossless FLAC) in a single
    invocation. Thread-safe rate-limited execution with automatic backoff retry.
    Returns (success, list_of_audio_file_paths, error_message).
    """
    ensure_spotiflac_extensions()
    output_dir.mkdir(parents=True, exist_ok=True)

    active_services = services if (services and len(services) > 0) else [
        "ext:tidal-web",
        "ext:qobuz-web",
        "ext:deezer",
        "ext:soundcloud",
        "ext:ytmusic-spotiflac",
    ]

    cmd = [
        "spotiflac",
        spotify_album_url,
        str(output_dir),
        "--quality",
        quality,
        "--service",
        *active_services,
        "--retries",
        "1",
        "--filename-format",
        "{track}. {title}",
    ]

    proc_env = {
        **os.environ,
        "SPOTIFLAC_REGISTRIES": DEFAULT_REGISTRY_URL,
        "CHROME_PATH": os.environ.get("CHROME_PATH", "/usr/bin/chromium"),
        "DISPLAY": os.environ.get("DISPLAY", ":99"),
    }

    last_error = ""

    for attempt in range(1, max_retries + 1):
        with _spotiflac_lock:
            _pace_spotiflac_call(rate_limit_delay)
            logger.info("[SPOTIFLAC] (Album attempt %d/%d) Running: %s", attempt, max_retries, " ".join(cmd))

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    env=proc_env,
                )

                try:
                    out, _ = proc.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    logger.warning("[SPOTIFLAC] Album process timed out after %ds for %s", timeout_seconds, spotify_album_url)
                    last_error = f"SpotiFLAC timed out after {timeout_seconds}s"
                    continue

                full_output = out or ""

                # Collect every audio file produced across subfolders
                audio_files = [f for f in output_dir.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]

                if audio_files:
                    logger.info("[SPOTIFLAC] Album download produced %d audio file(s): %s", len(audio_files), [f.name for f in audio_files])
                    return True, audio_files, None

                err_snippet = full_output[-500:].strip() if full_output else "No output"
                last_error = f"SpotiFLAC produced no audio files: {err_snippet}"

                # Check if rate limit / 429 error occurred in output
                if any(w in full_output.lower() for w in ("429", "rate limit", "too many requests", "throttle")):
                    logger.warning("[SPOTIFLAC] Rate limit detected from upstream provider on attempt %d. Backing off...", attempt)
                    time.sleep(3.0 * attempt)

            except Exception as e:
                logger.exception("[SPOTIFLAC] Album execution error for %s: %s", spotify_album_url, e)
                last_error = str(e)

        if attempt < max_retries:
            time.sleep(2.0 * attempt)

    logger.warning("[SPOTIFLAC] All %d album attempts failed for %s. Last error: %s", max_retries, spotify_album_url, last_error)
    return False, [], last_error
