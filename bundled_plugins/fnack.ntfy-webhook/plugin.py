"""Bundled first-party plugin: ntfy push notifications.

Same event subscriptions as the Discord plugin but posts to an ntfy server
(https://ntfy.sh or self-hosted). Two SEPARATE plugins (per HARNESS §3) so
trust tiers and permissions apply per service.
"""

import time

from plugins.base import EventHookPlugin

_DEBOUNCE_SECONDS = 60.0


class NtfyWebhookPlugin(EventHookPlugin):
    def on_load(self):
        self.context.events.subscribe("queue.job_completed", self._on_completed)
        self.context.events.subscribe("queue.job_failed", self._on_failed)
        self.context.events.subscribe("track.caution_flagged", self._on_caution)
        self._last_sent: dict = {}

    def _enabled(self, key: str) -> bool:
        return (self.context.settings.get(key, "true") or "true").lower() != "false"

    def _debounce(self, track_id, message: str) -> bool:
        now = time.time()
        key = (track_id, message[:80])
        last = self._last_sent.get(key)
        if last and now - last < _DEBOUNCE_SECONDS:
            return True
        self._last_sent[key] = now
        return False

    def _post(self, title: str, tags: str, message: str) -> None:
        topic = (self.context.settings.get("topic") or "").strip()
        if not topic:
            return
        server = (self.context.settings.get("server_url") or "https://ntfy.sh").rstrip("/")
        try:
            resp = self.context.http.post(
                f"{server}/{topic}",
                data=message.encode("utf-8"),
                headers={"Title": title, "Tags": tags},
                timeout=10,
            )
            if resp.status_code >= 500:
                time.sleep(5)
                self.context.http.post(f"{server}/{topic}", data=message.encode("utf-8"),
                                       headers={"Title": title, "Tags": tags}, timeout=10)
        except Exception:
            self.context.log.exception("ntfy POST failed")

    def _on_completed(self, **payload):
        if not self._enabled("on_job_completed"):
            return
        self._post(f"Downloaded: {payload.get('title')}", "white_check_mark",
                   f"{payload.get('artist_name')} — {payload.get('album_name')}")

    def _on_failed(self, **payload):
        if not self._enabled("on_job_failed"):
            return
        if self._debounce(payload.get("track_id"), payload.get("error") or ""):
            return
        self._post(f"Download failed: {payload.get('title')}", "x",
                   f"{payload.get('artist_name')}\n{(payload.get('error') or '')[:300]}")

    def _on_caution(self, **payload):
        if not self._enabled("on_caution_flagged"):
            return
        self._post("AcoustID flag", "warning",
                   f"Matched to: {payload.get('matched_title')} by {payload.get('matched_artist')} "
                   f"(score {payload.get('score')})")
