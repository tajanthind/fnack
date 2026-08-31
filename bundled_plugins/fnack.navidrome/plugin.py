"""Bundled first-party plugin: Navidrome scan_trigger (+ settings via the
standard schema modal — Brief 6 §2; no more custom settings_tab card).

The navidrome_service reads its config from global AppSetting rows
(navidrome_url/user/token/auto_scan/db_path). This plugin's namespaced
settings (schema keys url/user/token/auto_scan/db_path) are kept in sync
with those rows so existing behavior is unchanged — the schema modal is the
single settings surface, and on_settings_changed writes back to the global
rows the service still reads.
"""

from plugins.base import ScanTriggerPlugin
from services.navidrome_service import (
    test_navidrome_connection,
    trigger_navidrome_scan,
)

# schema key -> global AppSetting key
_SCHEMA_TO_GLOBAL = {
    "url": "navidrome_url",
    "user": "navidrome_user",
    "token": "navidrome_token",
    "auto_scan": "navidrome_auto_scan",
    "db_path": "navidrome_db_path",
}
_GLOBAL_TO_SCHEMA = {v: k for k, v in _SCHEMA_TO_GLOBAL.items()}


class NavidromePlugin(ScanTriggerPlugin):
    def _global_setting(self, key: str) -> str:
        return (self.context.library.get_setting(key, "") or "").strip()

    def _settings_from_global(self):
        """One-time: pull legacy AppSetting values into the plugin store
        (schema-keyed)."""
        for schema_key, global_key in _SCHEMA_TO_GLOBAL.items():
            if not self.context.settings.get(schema_key):
                val = self._global_setting(global_key)
                if val:
                    self.context.settings.set(schema_key, val)

    def _settings_to_global(self):
        """Write plugin settings (schema-keyed) back to the global rows the
        navidrome_service still reads."""
        for schema_key, global_key in _SCHEMA_TO_GLOBAL.items():
            val = (self.context.settings.get(schema_key) or "").strip()
            self.context.library.set_setting(global_key, val)

    def on_load(self):
        self._settings_from_global()

    def on_settings_changed(self, settings: dict):
        self._settings_to_global()

    def trigger_scan(self) -> tuple[bool, str]:
        try:
            from flask import current_app
            return trigger_navidrome_scan(current_app._get_current_object())
        except Exception:
            return False, "Navidrome scan failed (no app context)"

    def test_connection(self) -> tuple[bool, str]:
        url = self.context.settings.get("url") or self._global_setting("navidrome_url")
        user = self.context.settings.get("user") or self._global_setting("navidrome_user")
        token = self.context.settings.get("token") or self._global_setting("navidrome_token")
        return test_navidrome_connection(url, user, token)
