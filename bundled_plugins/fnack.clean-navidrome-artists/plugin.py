"""Bundled first-party plugin: 'Clean Navidrome Artists' library task.

Wraps scripts/clean_navidrome_artists.main() 1:1 — the same code the CLI
and run_maintenance.py invoke, so behavior is identical.
"""

from plugins.base import LibraryTaskPlugin, TaskResult


class CleanNavidromeArtistsTask(LibraryTaskPlugin):
    schedule = None  # manual-trigger-only (maintenance panel)

    def run(self) -> TaskResult:
        from scripts.clean_navidrome_artists import main
        code = main()
        return TaskResult(success=(code == 0), message=f"clean_navidrome_artists exit {code}")
