"""Bundled first-party plugin: Navidrome scan_trigger + settings_tab.

The navidrome_service reads its config from global AppSetting rows
(navidrome_url/user/token/auto_scan/db_path). The plugin keeps its
namespaced settings in sync with those rows so existing behavior is
unchanged (queue_service calls trigger_navidrome_scan(app) directly; that
function still reads the global rows). The settings_tab slot renders the
Navidrome config panel from this plugin's settings.
"""

from plugins.base import ScanTriggerPlugin
from services.navidrome_service import (
    test_navidrome_connection,
    trigger_navidrome_scan,
)

_KEYS = ("navidrome_url", "navidrome_user", "navidrome_token",
         "navidrome_auto_scan", "navidrome_db_path")


class NavidromePlugin(ScanTriggerPlugin):
    def _global_setting(self, key: str) -> str:
        return (self.context.library.get_setting(key, "") or "").strip()

    def _settings_from_global(self):
        """One-time: pull legacy AppSetting values into the plugin store."""
        for key in _KEYS:
            if not self.context.settings.get(key):
                val = self._global_setting(key)
                if val:
                    self.context.settings.set(key, val)

    def _settings_to_global(self):
        """Write plugin settings back to the global rows (source of truth for
        the existing navidrome_service)."""
        for key in _KEYS:
            val = (self.context.settings.get(key) or "").strip()
            self.context.library.set_setting(key, val)

    def on_load(self):
        self._settings_from_global()
        self.context.ui.register_slot("settings_tab", self._render_settings_tab)

    def on_settings_changed(self, settings: dict):
        self._settings_to_global()

    def trigger_scan(self) -> tuple[bool, str]:
        try:
            from flask import current_app
            return trigger_navidrome_scan(current_app._get_current_object())
        except Exception:
            return False, "Navidrome scan failed (no app context)"

    def test_connection(self) -> tuple[bool, str]:
        url = self.context.settings.get("navidrome_url") or self._global_setting("navidrome_url")
        user = self.context.settings.get("navidrome_user") or self._global_setting("navidrome_user")
        token = self.context.settings.get("navidrome_token") or self._global_setting("navidrome_token")
        return test_navidrome_connection(url, user, token)

    def _render_settings_tab(self, context_data: dict) -> str:
        url = self.context.settings.get("navidrome_url") or self._global_setting("navidrome_url")
        user = self.context.settings.get("navidrome_user") or self._global_setting("navidrome_user")
        db_path = self.context.settings.get("navidrome_db_path") or self._global_setting("navidrome_db_path")
        return f"""
<div class="card bg-dark-card border-0 shadow-sm mb-4">
  <div class="card-body p-4">
    <h5 class="fw-bold m-0"><i class="fas fa-server text-danger me-2"></i>Navidrome Integration</h5>
    <p class="text-secondary small mb-3">Configure scan triggering and album-split repair. Managed by the bundled Navidrome plugin.</p>
    <form onsubmit="savePluginSettings('fnack.navidrome'); return false;">
      <div class="mb-3">
        <label class="form-label small text-secondary">Server URL</label>
        <input type="text" class="form-control" id="plugin-fnack.navidrome-navidrome_url" value="{url}" placeholder="http://192.168.1.10:4533">
      </div>
      <div class="mb-3">
        <label class="form-label small text-secondary">Username</label>
        <input type="text" class="form-control" id="plugin-fnack.navidrome-navidrome_user" value="{user}" placeholder="admin">
      </div>
      <div class="mb-3">
        <label class="form-label small text-secondary">Password / Token</label>
        <input type="password" class="form-control" id="plugin-fnack.navidrome-navidrome_token" placeholder="••••••••">
      </div>
      <div class="mb-3">
        <label class="form-label small text-secondary">Navidrome DB path (album-split repair)</label>
        <input type="text" class="form-control" id="plugin-fnack.navidrome-navidrome_db_path" value="{db_path}" placeholder="/mnt/storage/media/config-navidrome/navidrome.db">
      </div>
      <button type="submit" class="btn btn-brand btn-sm">Save Navidrome Settings</button>
    </form>
  </div>
</div>"""
