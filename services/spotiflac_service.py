"""SpotiFLAC CLI service: Downloads true lossless FLAC without authentication via Tidal/Qobuz/SoundCloud/Deezer."""

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("fnack.spotiflac")

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac"}
DEFAULT_REGISTRY_URL = "https://raw.githubusercontent.com/spotiflacapp/SpotiFLAC-Extension/refs/heads/main/registry.json"

_initialized = False


def ensure_xvfb() -> None:
    """Ensure Xvfb virtual framebuffer display is active on :99 for headless browser solving."""
    try:
        res = subprocess.run(["pgrep", "-f", "Xvfb :99"], capture_output=True)
        if res.returncode != 0:
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
) -> Tuple[bool, Optional[Path], Optional[str]]:
    """
    Download a single track using SpotiFLAC (zero-auth lossless FLAC).
    Returns (success, output_file_path, error_message).
    """
    ensure_spotiflac_extensions()
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "spotiflac",
        spotify_url,
        str(output_dir),
        "--quality",
        quality,
        "--service",
        "ext:tidal-web",
        "ext:qobuz-web",
        "ext:deezer",
        "ext:soundcloud",
        "ext:ytmusic-spotiflac",
        "--retries",
        "1",
        "--filename-format",
        "{track}. {title}",
    ]

    logger.info("[SPOTIFLAC] Running: %s", " ".join(cmd))

    proc_env = {
        **os.environ,
        "SPOTIFLAC_REGISTRIES": DEFAULT_REGISTRY_URL,
        "CHROME_PATH": os.environ.get("CHROME_PATH", "/usr/bin/chromium"),
        "DISPLAY": os.environ.get("DISPLAY", ":99"),
    }

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
            return False, None, f"SpotiFLAC timed out after {timeout_seconds}s"

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
        logger.warning("[SPOTIFLAC] Download failed for %s. Log snippet: %s", spotify_url, err_snippet)
        return False, None, f"SpotiFLAC produced no audio file: {err_snippet}"

    except Exception as e:
        logger.exception("[SPOTIFLAC] Execution error for %s: %s", spotify_url, e)
        return False, None, str(e)
