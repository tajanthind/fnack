"""Bundled first-party plugin: Discord webhook notifications.

Subscribes to queue.job_completed / queue.job_failed / track.caution_flagged
and POSTs a Discord embed. Fire-and-forget (never blocks the queue); 5xx
retried once after 5s; 4xx accepted (bad webhook URL). Debounces identical
job_failed messages for the same track within 60s.
"""

import json
import time

from plugins.base import EventHookPlugin

_COLORS = {"ok": 0x2ECC71, "fail": 0xE74C3C, "caution": 0xF1C40F}
_DEBOUNCE_SECONDS = 60.0


class DiscordWebhookPlugin(EventHookPlugin):
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

    def _post(self, title: str, color: int, fields: list, url: str) -> None:
        payload = {
            "username": self.context.settings.get("username") or "fnack",
            "embeds": [{"title": title, "color": color, "fields": [
                {"name": f["name"], "value": f["value"][:1024], "inline": True} for f in fields
            ]}],
        }
        try:
            resp = self.context.http.post(url, json=payload, timeout=10)
            if resp.status_code >= 500:
                time.sleep(5)
                self.context.http.post(url, json=payload, timeout=10)
        except Exception:
            self.context.log.exception("Discord webhook POST failed")

    def _fields(self, **kw) -> list:
        return [{"name": k.replace("_", " ").title(), "value": str(v) or "—"}
                for k, v in kw.items() if v is not None]

    def _on_completed(self, **payload):
        if not self._enabled("on_job_completed"):
            return
        url = self.context.settings.get("webhook_url")
        if not url:
            return
        self._post(f"✅ Downloaded: {payload.get('title')}",
                   _COLORS["ok"],
                   self._fields(artist=payload.get("artist_name"), album=payload.get("album_name")),
                   url)

    def _on_failed(self, **payload):
        if not self._enabled("on_job_failed"):
            return
        url = self.context.settings.get("webhook_url")
        if not url:
            return
        if self._debounce(payload.get("track_id"), payload.get("error") or ""):
            return
        self._post(f"❌ Download failed: {payload.get('title')}",
                   _COLORS["fail"],
                   self._fields(artist=payload.get("artist_name"), error=(payload.get("error") or "")[:100]),
                   url)

    def _on_caution(self, **payload):
        if not self._enabled("on_caution_flagged"):
            return
        url = self.context.settings.get("webhook_url")
        if not url:
            return
        self._post(f"⚠️ AcoustID flag: {payload.get('title', 'track')}",
                   _COLORS["caution"],
                   self._fields(matched_to=payload.get("matched_title"), artist=payload.get("matched_artist"),
                                score=payload.get("score")),
                   url)
