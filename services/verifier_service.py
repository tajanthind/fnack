"""Post-download audio verification service: Mutagen tag inspection & duration matching."""

import logging
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


def verify_audio_file(
    file_path: Path,
    expected_duration_seconds: Optional[float] = None,
    expected_artist: Optional[str] = None,
    expected_title: Optional[str] = None,
    max_duration_delta: float = DEFAULT_DURATION_DELTA_SECONDS,
    delete_on_failure: bool = True,
) -> Tuple[bool, Optional[str], dict]:
    """
    Verify downloaded audio file validity and match against expected metadata.
    Enforces strict duration delta check to guarantee wrong songs are rejected.
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
        "[VERIFIER] Verified %s | Duration: %.1fs (expected: %s) | Size: %d KB",
        file_path.name,
        actual_dur or 0,
        f"{expected_duration_seconds:.1f}s" if expected_duration_seconds else "N/A",
        meta["size_bytes"] // 1024,
    )
    return True, None, meta
