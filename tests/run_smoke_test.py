"""End-to-end smoke test for the fnack plugin framework.

Exercises, against the REAL fnack models (models.py) in an in-memory SQLite:
manifest loading + api_version compatibility check, dynamic import of the
bundled example plugin, context injection, event subscription and emission,
`library.mark_caution()` writing back through the ORM, UI-slot registration and
rendering, the settings REST endpoint, and enable/disable.

Run from the repo root:

    .venv/bin/python tests/run_smoke_test.py

Expected output ends with `SMOKE TEST PASSED`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask

from models import Album, Artist, Track, db

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

with app.app_context():
    import plugins.models  # noqa: F401 — registers the plugin tables with `db`
    db.create_all()

    # Real fnack Track requires an Album (and an Artist for the relationship).
    artist = Artist(spotify_id="test-artist-1", name="Test Artist")
    db.session.add(artist)
    db.session.flush()
    album = Album(artist_id=artist.id, name="Test Album")
    db.session.add(album)
    db.session.flush()
    t = Track(album_id=album.id, artist_id=artist.id, title="Test Song", bitrate=128)
    db.session.add(t)
    db.session.commit()
    track_id = t.id

    from plugins.manager import init_plugin_manager
    from version import __version__

    manager = init_plugin_manager(
        plugins_dir=str(Path(__file__).resolve().parent.parent / "examples" / "plugins"),
        core_version=__version__,
    )
    manager.load_all()  # no enabled_ids filter -> enable everything discovered
    print("Loaded plugins:", manager.list_loaded())
    assert manager.list_loaded()[0]["enabled"] is True, "plugin should be enabled"
    assert manager.list_loaded()[0]["id"] == "dev.fnack.example-quality-flag", (
        "expected the bundled example plugin to be discovered"
    )

    # Simulate the queue emitting the after-download event (INTEGRATION.md step 5)
    manager.event_bus.emit("track.after_download", track_id=track_id)

    refreshed = db.session.get(Track, track_id)
    print("Track caution:", refreshed.caution, "-", refreshed.caution_info)
    assert refreshed.caution is True, "low-bitrate track should have been flagged"

    # Exercise the UI slot rendering path
    html = manager.get_ui_slot_html("track_row_actions", {
        "track": {"caution": refreshed.caution, "caution_info": refreshed.caution_info}
    })
    print("Rendered slot HTML:", html)
    assert "Low quality" in html

    # Exercise settings get/set + on_settings_changed
    from plugins.api import build_plugins_blueprint
    from plugins.registry import PluginRegistry

    registry = PluginRegistry(manager)
    bp = build_plugins_blueprint(manager, registry)
    app.register_blueprint(bp)

    client = app.test_client()
    r = client.post("/api/plugins/dev.fnack.example-quality-flag/settings", json={"min_bitrate_kbps": 320})
    print("Settings POST:", r.status_code, r.json)
    assert r.status_code == 200

    r = client.get("/api/plugins")
    print("List installed:", r.status_code, r.json)
    assert r.status_code == 200

    r = client.post("/api/plugins/dev.fnack.example-quality-flag/disable")
    print("Disable:", r.status_code, r.json)
    assert manager.list_loaded()[0]["enabled"] is False

print("\nSMOKE TEST PASSED")
