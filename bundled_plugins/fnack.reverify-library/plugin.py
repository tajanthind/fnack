"""Bundled first-party plugin: 'Reverify Library' library task."""

from plugins.base import LibraryTaskPlugin, TaskResult


class ReverifyLibraryTask(LibraryTaskPlugin):
    schedule = None  # manual-trigger-only (maintenance panel)

    def run(self) -> TaskResult:
        from scripts.reverify_library import main
        code = main()
        return TaskResult(success=(code == 0), message=f"reverify_library exit {code}")
