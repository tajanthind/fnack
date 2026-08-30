"""Bundled first-party plugin: AcoustID fingerprinting.

SAFETY-RELEVANT behavior preserved exactly (wayfinder tickets
acoustid-fingerprinting.md + regional-artist-fallback.md):
- keyless (no api_key) => disabled, silent no-op
- verify-when-unsure at the 0.8 gate stays in core's _verify_or_rescue
  (queue_service), which consults this plugin via the chain
- regional no-match changes nothing; confirmed mismatch => caution flag

The key is stored in BOTH this plugin's namespaced setting and the legacy
`acoustid_api_key` AppSetting row (the service still reads the global row;
the plugin keeps them in sync so existing behavior is unchanged).
"""

from pathlib import Path
from typing import Optional

from plugins.base import FingerprintPlugin, FingerprintResult
from services.acoustid_service import identify, is_enabled


class AcoustIDFingerprinter(FingerprintPlugin):
    def _sync_key_from_global(self):
        """Read the legacy global AppSetting value into the plugin setting
        (one-time migration for users who had a key set before Phase 1)."""
        try:
            from models import AppSetting, db
            old = db.session.get(AppSetting, "acoustid_api_key")
            if old and old.value and not self.context.settings.get("api_key"):
                self.context.settings.set("api_key", old.value)
        except Exception:
            pass

    def _sync_key_to_global(self):
        """Write the plugin setting back to the legacy AppSetting row so
        services.acoustid_service.is_enabled()/identify() keep working."""
        try:
            from models import AppSetting, db
            key = (self.context.settings.get("api_key") or "").strip()
            row = db.session.get(AppSetting, "acoustid_api_key")
            if row is None:
                db.session.add(AppSetting(key="acoustid_api_key", value=key))
            else:
                row.value = key
            db.session.commit()
        except Exception:
            pass

    def on_load(self):
        self._sync_key_from_global()

    def on_settings_changed(self, settings: dict):
        self._sync_key_to_global()

    def is_enabled(self) -> bool:
        return is_enabled()

    def identify(self, file_path: Path) -> FingerprintResult:
        """Manual 'Identify this file' flow — returns the top candidate."""
        candidates = identify(str(file_path))
        if not candidates:
            return FingerprintResult(confidence=0.0)
        top = candidates[0]
        return FingerprintResult(
            confidence=float(top.get("score") or 0.0),
            matched_title=top.get("title"),
            matched_artist=(top.get("artists") or [None])[0],
            raw=top,
        )
