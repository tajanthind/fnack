"""Bundled first-party plugin: Navidrome media-server integration.

AUTHORITATIVE implementation (Phase 4): the Navidrome-specific logic — ping
connection test, debounced scan trigger — lives in
this plugin's `navidrome.py` (moved from the deleted
`services/navidrome_service.py`). All config (url/user/token/auto_scan/
db_path) is PLUGIN-OWNED via the standard settings schema; the plugin
injects it into the module (no core AppSetting reads). Core never imports a
Navidrome implementation.

Serves the media.scan / media.health / media.connection_test capabilities
(resolved by MediaServerService).
"""

from typing import Optional

from plugins.base import ScanTriggerPlugin

# Multi-file plugin (Phase 2): the manager puts this plugin's dir on sys.path,
# so the sibling provider module imports by name.
import navidrome  # noqa: E402


class NavidromePlugin(ScanTriggerPlugin):
    def _config(self) -> dict:
        """Plugin-owned settings (schema keys url/user/token/auto_scan/db_path)."""
        return {
            "url": (self.context.settings.get("url") or "").strip(),
            "user": (self.context.settings.get("user") or "").strip(),
            "token": (self.context.settings.get("token") or "").strip(),
            "auto_scan": str(self.context.settings.get("auto_scan", "true")),
            "db_path": (self.context.settings.get("db_path") or "").strip(),
        }

    def on_load(self) -> None:
        # One-time migration: pull legacy AppSetting values into the plugin
        # store (schema-keyed), then the plugin setting is authoritative.
        legacy_map = {
            "url": "navidrome_url",
            "user": "navidrome_user",
            "token": "navidrome_token",
            "auto_scan": "navidrome_auto_scan",
            "db_path": "navidrome_db_path",
        }
        for schema_key, global_key in legacy_map.items():
            if not self.context.settings.get(schema_key):
                val = (self.context.library.get_setting(global_key, "") or "").strip()
                if val:
                    self.context.settings.set(schema_key, val)

    def trigger_scan(self) -> tuple[bool, str]:
        return navidrome.trigger_navidrome_scan(self._config())

    def test_connection(self, candidate_config: Optional[dict] = None) -> tuple[bool, str]:
        """Test the connection — STORED config, optionally overridden by the
        caller's UNSAVED candidate values (Phase 3 §Candidate configuration:
        the settings UI validates typed-but-not-saved values through the
        application service)."""
        cfg = self._config()
        if candidate_config:
            cfg.update({k: v for k, v in candidate_config.items() if v is not None})
        url = cfg.get("url") or ""
        user = cfg.get("user") or ""
        token = cfg.get("token") or ""
        return navidrome.test_navidrome_connection(url, user, token)

    def health(self) -> dict:
        """media.health: reachability + auth status of the configured server."""
        ok, msg = navidrome.test_navidrome_connection(
            self._config().get("url") or "",
            self._config().get("user") or "",
            self._config().get("token") or "",
        )
        return {"ok": ok, "message": msg, "configured": bool(self._config().get("url"))}
