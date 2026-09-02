"""Bundled first-party plugin: AcoustID fingerprinting.

AUTHORITATIVE implementation (Phase 4): the AcoustID-specific logic —
fingerprint via fpcalc, lookup with the 1.2s pacing, match/mismatch
verification, and the manual identify flow — lives in this plugin's
`acoustid.py` (moved from the deleted `services/acoustid_service.py`). The
API key is PLUGIN-OWNED (api_key setting; injected via set_api_key()); the
plugin serves the fingerprint.identify capability and exposes the
verification + manual-identify helpers the core route uses through the
plugin boundary. Core never imports an AcoustID implementation.

SAFETY-RELEVANT behavior preserved exactly (wayfinder tickets
acoustid-fingerprinting.md + regional-artist-fallback.md):
- keyless (no api_key) => disabled, silent no-op
- verify-when-unsure at the 0.8 gate stays core policy (VerificationService
  consumes this plugin's normalized evidence via FingerprintService)
- regional no-match changes nothing; confirmed mismatch => caution flag
"""

from pathlib import Path
from typing import Optional

from plugins.base import FingerprintPlugin, FingerprintResult

# Multi-file plugin (Phase 2): the manager puts this plugin's dir on sys.path,
# so the sibling provider module imports by name.
import acoustid  # noqa: E402


class AcoustIDFingerprinter(FingerprintPlugin):
    def on_load(self) -> None:
        # Plugin-owned key (Phase 4): migrate the legacy global once.
        try:
            old_val = self.context.library.get_setting("acoustid_api_key")
            if old_val and not self.context.settings.get("api_key"):
                self.context.settings.set("api_key", old_val)
        except Exception:
            pass
        acoustid.set_api_key(self.context.settings.get("api_key") or "")

    def on_settings_changed(self, settings: dict):
        acoustid.set_api_key((settings.get("api_key") or "").strip())

    def is_enabled(self) -> bool:
        return acoustid.is_enabled()

    def identify(self, file_path: Path) -> FingerprintResult:
        """Manual 'Identify this file' flow — returns the top candidate."""
        candidates = acoustid.identify(str(file_path))
        if not candidates:
            return FingerprintResult(confidence=0.0)
        top = candidates[0]
        return FingerprintResult(
            confidence=float(top.get("score") or 0.0),
            matched_title=top.get("title"),
            matched_artist=(top.get("artists") or [None])[0],
            raw=top,
        )

    # -- helpers the core manual-identify route uses (via the plugin) --------

    def identify_candidates(self, path: str) -> list:
        return acoustid.identify(path)

    def last_lookup_flags(self) -> dict:
        return {
            "had_results": acoustid._last_lookup_had_results,
            "missing_metadata": acoustid._last_lookup_missing_metadata,
        }

    def verify_download(self, path: str, expected_artist: Optional[str],
                        expected_title: Optional[str],
                        expected_duration: Optional[float]) -> dict:
        return acoustid.verify_download(path, expected_artist, expected_title,
                                        expected_duration)
