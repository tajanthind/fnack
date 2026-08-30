"""Bundled first-party plugin: 'Fix Navidrome Splits' library task."""

from plugins.base import LibraryTaskPlugin, TaskResult


class FixNavidromeSplitsTask(LibraryTaskPlugin):
    schedule = None  # manual-trigger-only (maintenance panel)

    def run(self) -> TaskResult:
        from scripts.fix_navidrome_splits import main
        code = main()
        return TaskResult(success=(code == 0), message=f"fix_navidrome_splits exit {code}")
