"""Template plugin — copy this directory as a starting point.

Demonstrates, in the smallest useful example, the exact mechanism you
described for the AcoustID-style caution badge, generalized: any plugin can
flag a track (`context.library.mark_caution`) and contribute a small UI
fragment that reads that flag (`context.ui.register_slot`).

This plugin is intentionally not a real feature — it just checks bitrate
against a configurable threshold on the `track.after_download` event.
"""

from plugins.base import EventHookPlugin


class QualityFlagPlugin(EventHookPlugin):

    def on_load(self):
        self.context.events.subscribe("track.after_download", self._on_track_downloaded)
        self.context.ui.register_slot("track_row_actions", self._render_badge)

    def _min_bitrate(self) -> int:
        return int(self.context.settings.get("min_bitrate_kbps", 256))

    def _on_track_downloaded(self, track_id: int, **_kwargs):
        track = self.context.library.get_track(track_id)
        if not track or not track.get("bitrate"):
            return
        threshold = self._min_bitrate()
        if track["bitrate"] < threshold:
            self.context.library.mark_caution(
                track_id,
                f"Bitrate {track['bitrate']}kbps is below your {threshold}kbps threshold",
            )
            self.context.log.info("Flagged track %s: low bitrate", track_id)

    def _render_badge(self, context_data: dict) -> str:
        track = context_data.get("track") or {}
        if not track.get("caution"):
            return ""
        reason = track.get("caution_info", "Quality warning")
        return (
            f'<span class="badge bg-warning text-dark" title="{reason}">'
            f'<i class="fa-solid fa-triangle-exclamation"></i> Low quality</span>'
        )

    def on_settings_changed(self, settings: dict):
        # Nothing to re-run immediately — the new threshold just applies to
        # the next track.after_download event.
        self.context.log.info("Threshold updated to %s", settings.get("min_bitrate_kbps"))
