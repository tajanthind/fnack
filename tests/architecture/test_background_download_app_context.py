"""Regression test: P0 app-context fix (final-cleanup review round).

The P0 was that provider invocations in the background download path ran
outside a Flask app context: `_process_track_job` / `download_manual_match_track`
/ `_sync_artist_discography_background` run on a ThreadPoolExecutor worker /
SocketIO greenlet where the app context is NOT inherited, and an earlier
head-only `with app.app_context():` left the resolve/download/enrich provider
chain context-free — so any provider reading plugin settings via the DB
(`PluginContext.settings.get`) crashed with "Working outside of application
context" and every download failed.

The fix restructured each function so its ENTIRE body sits under ONE
`with app.app_context():` (by construction). This test:

1. Structural: scans the three functions and asserts no body line after the
   single `with app.app_context():` escapes to indent < 8 (nothing can run
   outside the context).
2. Runtime: executes the ACTUAL background download path — `_process_track_job`
   submitted to a real ThreadPoolExecutor worker (a fresh thread, so no app
   context is inherited) — with a downloader fixture whose `download()` reads
   `self.context.settings.get("timeout")` FIRST (the exact line that crashed
   in production). The settings read must succeed in the worker thread; the
   fixture records it by writing a marker file. No "Working outside of
   application context" anywhere.

Run from the repo root:

    .venv/bin/python tests/architecture/test_background_download_app_context.py
"""

import concurrent.futures
import re
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask  # noqa: E402

from models import Album, Artist, DownloadJob, Track, db  # noqa: E402

FUNCTIONS = {
    "services/queue_service.py": {
        "def _process_track_job(": None,
        "def download_manual_match_track(": None,
    },
    "app.py": {
        "def _sync_artist_discography_background(": None,
    },
}


def test_entire_bodies_sit_under_one_app_context() -> None:
    """Structural guarantee: after each function's single
    `with app.app_context():`, every non-blank body line is indented at least
    8 spaces — nothing in the function body can run without the context."""
    for rel, defs in FUNCTIONS.items():
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        for d in defs:
            start = next(i for i, l in enumerate(lines) if l.startswith(d))
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].startswith("def ") and i != start), len(lines))
            with_i = next(i for i in range(start, end)
                          if "with app.app_context():" in lines[i])
            escaped = [
                i + 1 for i in range(with_i + 1, end)
                if lines[i].strip() and not lines[i].startswith("        ")
                and not lines[i].lstrip().startswith("@")
            ]
            assert not escaped, (
                f"{rel} {d.strip()}: body lines after the app-context `with` "
                f"are NOT inside it: {escaped} — the whole body must be "
                "indented under the single `with app.app_context():`"
            )


def _test_runtime_background_download() -> None:
    """Drive the real `_process_track_job` on a ThreadPoolExecutor worker with
    a downloader whose `download()` reads plugin settings via the DB."""
    from flask import Flask as _Flask

    app = _Flask(__name__)
    # Shared-cache in-memory SQLite: the ThreadPoolExecutor worker (a real
    # thread) must see the same DB the test main thread writes.
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///file:fnack_ctx_{threading.get_ident()}?mode=memory&cache=shared&uri=true"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        import plugins.models  # noqa: F401 — register plugin tables
        db.create_all()

        # Fixture plugin: download() reads context.settings FIRST (the exact
        # ytdlp crash line), records success by writing a marker, then fails
        # cleanly (no real audio) so the job ends without touching the
        # library.
        fixture = ROOT / "tests" / "bundled_fixture" / "fnack.testctx"
        fixture.mkdir(parents=True, exist_ok=True)
        (fixture / "plugin.json").write_text(
            '{"id":"fnack.testctx","name":"TestCtx","version":"1.0.0",'
            '"type":["downloader"],"api_version":"^1.0","min_core_version":"0.2.0",'
            '"entry_point":"plugin:TestCtxDownloader","author":"fnack",'
            '"description":"app-context regression fixture","permissions":[],'
            '"settings_schema":[{"key":"timeout","type":"number","default":"180"}],'
            '"ui":{"slots":[]},"dependencies":{},"trust_level":"official"}'
        )
        marker = Path(tempfile.gettempdir()) / f"testctx_marker_{threading.get_ident()}"
        marker.unlink(missing_ok=True)
        (fixture / "plugin.py").write_text(
            "from pathlib import Path\n"
            "from plugins.base import PluginBase\n"
            "from fnack.plugin_api.providers import TrackDownloader\n"
            "from fnack.plugin_api.models import DownloadRequest, DownloadResult\n"
            "class TestCtxDownloader(PluginBase, TrackDownloader):\n"
            "    priority = 10\n"
            "    async def can_handle(self, request): return True\n"
            "    async def download(self, request: DownloadRequest):\n"
            "        # EXACT production crash line: settings read through the DB.\n"
            "        timeout = int(self.context.settings.get('timeout') or 180)\n"
            f"        Path(r'{marker}').write_text(str(timeout))\n"
            "        return DownloadResult(provider_id='fnack.testctx', success=False,\n"
            "                              message='no audio fixture')\n"
        )

        from plugins.manager import init_plugin_manager
        manager = init_plugin_manager(
            plugins_dir=str(ROOT / "examples" / "plugins"),
            bundled_plugins_dir=str(fixture.parent),
            core_version="0.3.21",
        )
        manager.load_all()

        # Artist -> Album -> Track -> queued DownloadJob (worker picks it up).
        from models import AppSetting
        import tempfile as _tempfile
        _tmp_music = Path(_tempfile.mkdtemp(prefix="fnack-music-"))
        db.session.add(AppSetting(key="music_path", value=str(_tmp_music)))
        artist = Artist(spotify_id="ctx-test", name="Ctx Artist")
        db.session.add(artist)
        db.session.flush()
        album = Album(artist_id=artist.id, name="Ctx Album")
        db.session.add(album)
        db.session.flush()
        track = Track(album_id=album.id, artist_id=artist.id, title="Ctx Song")
        db.session.add(track)
        db.session.flush()
        job = DownloadJob(track_id=track.id, album_id=album.id,
                          artist_id=artist.id, album_name="Ctx Album",
                          status="downloading")
        db.session.add(job)
        db.session.commit()
        job_id = job.id

        from services import queue_service
        queue_service.DOWNLOADS_DIR = Path(_tempfile.mkdtemp(prefix="fnack-dl-"))

        class _Sock:
            def emit(self, *a, **k):
                pass

        # Production mechanism: real thread pool, fresh thread => NO inherited
        # app context. The fix's single function-level context must cover the
        # provider chain.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(queue_service._process_track_job, app, _Sock(), job_id)
            fut.result(timeout=60)

        # The settings read executed inside the worker (marker written) — the
        # provider did NOT crash with "Working outside of application context".
        # (The job row is left 'downloading' because this test drives
        # `_process_track_job` directly, bypassing the worker loop's
        # failure-bookkeeping — irrelevant to the assertion: the settings read
        # either completed in the worker or the provider raised, and the
        # marker proves it completed.)
        assert marker.exists(), (
            "provider settings read did not complete in the background worker — "
            "the app-context fix is broken (head-only context regression)"
        )
        print(f"[background download] provider context.settings.get() OK inside the "
              f"worker thread (timeout={marker.read_text()})")
        marker.unlink(missing_ok=True)


if __name__ == "__main__":
    test_entire_bodies_sit_under_one_app_context()
    _test_runtime_background_download()
    print("test_background_download_app_context: PASSED")
