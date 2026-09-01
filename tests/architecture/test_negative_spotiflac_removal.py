"""Negative architectural test: the downloader chain works with fnack.spotiflac
disabled/uninstalled (Phase 2, PR 3 regression fence).

The point of the extraction is that SpotiFLAC is ONE download.track provider
among many. If it is disabled or uninstalled, the chain must still function
through the remaining provider (fnack.ytdlp) with ZERO SpotiFLAC-specific
branch/import in core. This test proves:

1. With fnack.spotiflac DISABLED: download.track is served only by ytdlp,
   get_downloaders() returns ytdlp, and has_capability("download.track")
   is still True.
2. The chain's migration adapter invokes ytdlp (legacy contract) and returns
   a legacy-shaped result — no SpotiFLAC involvement.
3. With fnack.spotiflac UNINSTALLED (tombstone) + reload: spotiflac is not
   even loaded, yet download.track is still served by ytdlp.
4. Source-level: the download-chain code in queue_service.py contains no
   SpotiFLAC-specific branch or import (no `enable_spotiflac` gate, no
   engine-gate keyed by fnack.spotiflac, no services.spotiflac_service).

Run from the repo root:

    .venv/bin/python tests/architecture/test_negative_spotiflac_removal.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _runtime_deps_present() -> bool:
    try:
        import flask  # noqa: F401
        import flask_sqlalchemy  # noqa: F401
        import yt_dlp  # noqa: F401
        import services.ytdlp_service  # noqa: F401
        return True
    except ImportError:
        return False


def _make_app():
    """A minimal Flask app with an in-memory SQLite, mirroring the smoke
    test so bundled plugins' on_load (settings migration) runs for real."""
    from flask import Flask
    from models import db
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        import plugins.models  # noqa: F401 — register plugin tables
        db.create_all()
    return app


def _load_manager(tmpdir: str):
    from plugins.manager import init_plugin_manager
    return init_plugin_manager(
        plugins_dir=tmpdir,
        bundled_plugins_dir=str(ROOT / "bundled_plugins"),
        core_version="0.3.1",
    )


def test_chain_works_with_spotiflac_disabled() -> None:
    """Disable fnack.spotiflac; download.track must still be served by ytdlp
    and the chain adapter must invoke it (legacy contract)."""
    if not _runtime_deps_present():
        print("SKIPPED test_chain_works_with_spotiflac_disabled (runtime deps missing)")
        return
    import logging
    logging.disable(logging.WARNING)
    import tempfile
    from services.queue_service import (
        _invoke_downloader_can_handle,
        _invoke_downloader_download,
    )
    from plugins.base import TrackRef

    app = _make_app()
    with app.app_context():
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _load_manager(tmpdir)
            mgr.load_all()  # everything enabled
            assert mgr.capability_registry.has("download.track")
            before = [p.plugin_id for p in mgr.capability_registry.providers_for("download.track")]
            assert "fnack.spotiflac" in before and "fnack.ytdlp" in before

            # Disable SpotiFLAC — the plugin's capability disappears (MASTER rule 2).
            mgr.disable_plugin("fnack.spotiflac")
            after = [p.plugin_id for p in mgr.capability_registry.providers_for("download.track")]
            assert after == ["fnack.ytdlp"], f"expected only ytdlp, got {after}"
            assert mgr.capability_registry.has("download.track") is True, \
                "download.track must still be available without spotiflac"
            dl_ids = [getattr(d, "manifest", None).id if getattr(d, "manifest", None) else type(d).__name__
                      for d in mgr.get_downloaders()]
            assert dl_ids == ["fnack.ytdlp"], f"get_downloaders() must be ytdlp only, got {dl_ids}"

            # The chain adapter invokes ytdlp (legacy DownloaderPlugin contract).
            tr = TrackRef(id=1, title="Test Song", artist_name="Test Artist",
                          album_name="Test Album", isrc="QZNW72379756", duration=180.0)
            work = Path(tmpdir) / "work"
            work.mkdir(exist_ok=True)
            options = {"format": "opus", "audio_source": "youtube_music"}
            assert _invoke_downloader_can_handle(mgr, mgr.get_plugin("fnack.ytdlp"),
                                                 tr, work, options) is True, \
                "ytdlp can_handle must still work via the adapter"
            result = _invoke_downloader_download(mgr, mgr.get_plugin("fnack.ytdlp"),
                                                 tr, work, options, timeout=5)
            # ytdlp will fail fast offline (no network/yt-dlp), but the point is
            # the invocation path runs and returns a legacy-shaped result — no
            # SpotiFLAC involvement and no crash.
            assert result is not None and hasattr(result, "success"), \
                "adapter must return a legacy-shaped DownloadResult"
            print("  chain-with-spotiflac-disabled: ytdlp invoked via adapter, result.success =",
                  result.success)
    logging.disable(logging.NOTSET)


def test_chain_works_with_spotiflac_uninstalled() -> None:
    """Tombstone-uninstall fnack.spotiflac, reload: spotiflac is not loaded
    at all, yet download.track is still served by ytdlp."""
    if not _runtime_deps_present():
        print("SKIPPED test_chain_works_with_spotiflac_uninstalled (runtime deps missing)")
        return
    import logging
    logging.disable(logging.WARNING)
    import tempfile
    from models import AppSetting, db

    app = _make_app()
    with app.app_context():
        # Simulate the uninstall tombstone (what the uninstall endpoint writes).
        db.session.add(AppSetting(key="plugin.uninstalled.fnack.spotiflac", value="1"))
        db.session.commit()
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _load_manager(tmpdir)
            mgr.load_all()  # tombstone skips spotiflac during load_all
            assert mgr.get_loaded("fnack.spotiflac") is None, \
                "uninstalled spotiflac must not be loaded"
            providers = [p.plugin_id for p in mgr.capability_registry.providers_for("download.track")]
            assert providers == ["fnack.ytdlp"], f"expected only ytdlp, got {providers}"
            assert mgr.capability_registry.has("download.track") is True
            print("  chain-with-spotiflac-uninstalled: ytdlp serves download.track")
    logging.disable(logging.NOTSET)


def test_no_spotiflac_branch_in_chain_source() -> None:
    """Source-level: the download-chain code must not contain a SpotiFLAC-
    specific branch/import. If someone re-hardwires spotiflac into the chain
    (enable_spotiflac gate, engine-gate keyed by fnack.spotiflac, a
    services.spotiflac_service import, or a provider-named download helper),
    this test fails."""
    src = (ROOT / "services" / "queue_service.py").read_text(encoding="utf-8")
    forbidden = [
        "spotiflac_service",          # deleted service import
        '"fnack.spotiflac"',          # provider-ID keyed gate/branch
        "'fnack.spotiflac'",
        "enable_spotiflac",           # legacy SpotiFLAC-specific toggle
        "_download_via_spotiflac",    # provider-named manual-path helper
    ]
    for needle in forbidden:
        assert needle not in src, (
            f"queue_service.py must have no SpotiFLAC-specific branch/import "
            f"(found {needle!r}) — the chain must be provider-neutral"
        )
    # The chain must be capability-driven: it resolves providers via the
    # download.track capability, never by naming a provider.
    assert "download.track" in src


if __name__ == "__main__":
    test_chain_works_with_spotiflac_disabled()
    test_chain_works_with_spotiflac_uninstalled()
    test_no_spotiflac_branch_in_chain_source()
    print("test_negative_spotiflac_removal: PASSED")
