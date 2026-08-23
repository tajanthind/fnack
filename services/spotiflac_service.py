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

# 429 circuit breaker: when upstream providers keep returning 429, back off hard
# and let the queue skip SpotiFLAC in favor of the yt-dlp fallback until recovery.
_rate_limit_cooldown_until = 0.0
_consecutive_429s = 0
_rate_lock = threading.Lock()


def _on_rate_limit_detected() -> float:
    """Record a 429 and return the cool-down seconds to wait before the next attempt."""
    global _rate_limit_cooldown_until, _consecutive_429s
    with _rate_lock:
        _consecutive_429s += 1
        backoff = min(300.0, 30.0 * _consecutive_429s)
        _rate_limit_cooldown_until = time.time() + backoff
        return backoff


def _on_success() -> None:
    """Reset the 429 circuit breaker after a successful download."""
    global _consecutive_429s, _rate_limit_cooldown_until
    with _rate_lock:
        _consecutive_429s = 0
        _rate_limit_cooldown_until = 0.0


def is_spotiflac_rate_limited() -> bool:
    """True while the 429 cool-down is active (queue should prefer the yt-dlp fallback)."""
    return time.time() < _rate_limit_cooldown_until


def _wait_out_cooldown() -> float:
    """Return the remaining 429 cool-down seconds (sticky: the cool-down stays armed
    until its time elapses, so the queue's circuit-breaker check also sees it)."""
    with _rate_lock:
        if time.time() < _rate_limit_cooldown_until:
            return _rate_limit_cooldown_until - time.time()
        return 0.0


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


def _extract_failure_details(output: str, max_chars: int = 900) -> str:
    """Pull meaningful failure lines out of SpotiFLAC's raw CLI output.

    The CLI prints box-drawing tables and debug noise; this extracts the lines
    that explain WHY a download failed (provider errors, 429s, matches, etc.).
    """
    if not output:
        return "No output produced"
    interesting = []
    seen = set()
    for line in output.splitlines():
        low = line.strip()
        if not low:
            continue
        if any(k in low.lower() for k in (
            "✗", "failed", "429", "too many requests", "rate limit", "not available",
            "no match", "not_found", "is not defined", "error", "denied",
            "timed out", "timeout", "login", "authentication", "unavailable",
        )):
            # Strip box-drawing characters and leading bullets
            clean = low.replace("║", "").replace("═", "").replace("╔", "").replace("╗", "").replace("╚", "").replace("╝", "").replace("╠", "").replace("╣", "").strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            interesting.append(clean)
        if len(interesting) >= 6:
            break
    snippet = " | ".join(interesting) if interesting else output[-400:].strip()
    return snippet[:max_chars]


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
            cooldown_waited = _wait_out_cooldown()
            if cooldown_waited:
                logger.info("[SPOTIFLAC] Waited %.0fs for upstream 429 cool-down before retrying %s", cooldown_waited, spotify_url)
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
                    _on_success()
                    logger.info("[SPOTIFLAC] Successfully downloaded: %s (%d bytes)", latest_file.name, latest_file.stat().st_size)
                    return True, latest_file, None

                if audio_files:
                    latest_file = max(audio_files, key=lambda f: f.stat().st_mtime)
                    _on_success()
                    logger.info("[SPOTIFLAC] Output file found despite non-zero exit: %s", latest_file.name)
                    return True, latest_file, None

                err_snippet = _extract_failure_details(full_output)
                last_error = f"SpotiFLAC failed: {err_snippet}"
                logger.warning(
                    "[SPOTIFLAC] Download failed for %s (attempt %d/%d). Reasons: %s",
                    spotify_url, attempt, max_retries, err_snippet,
                )

                # Check if rate limit / 429 error occurred in output; open the circuit
                # breaker so the shared upstream proxies can recover before the next try
                if any(w in full_output.lower() for w in ("429", "rate limit", "too many requests", "throttle")):
                    backoff = _on_rate_limit_detected()
                    logger.warning("[SPOTIFLAC] Upstream rate limit (429) detected on attempt %d. Circuit breaker: pausing SpotiFLAC for %.0fs...", attempt, backoff)

            except Exception as e:
                logger.exception("[SPOTIFLAC] Execution error for %s: %s", spotify_url, e)
                last_error = str(e)

        if attempt < max_retries:
            time.sleep(2.0 * attempt)

    logger.warning("[SPOTIFLAC] All %d attempts failed for %s. Last error: %s", max_retries, spotify_url, last_error)
    return False, None, last_error
