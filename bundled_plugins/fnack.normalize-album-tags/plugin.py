"""Bundled first-party plugin: 'Normalize Album Tags' library task."""

from plugins.base import LibraryTaskPlugin, TaskResult


class NormalizeAlbumTagsTask(LibraryTaskPlugin):
    schedule = None  # manual-trigger-only (maintenance panel)

    def run(self) -> TaskResult:
        from scripts.normalize_album_tags import main
        code = main()
        return TaskResult(success=(code == 0), message=f"normalize_album_tags exit {code}")
