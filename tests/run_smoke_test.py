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

    # A fake "bundled" plugin dir so the Phase 3 bundled guards (install/
    # uninstall refusal) have a bundled id to match against.
    bundled_fixture = Path(__file__).resolve().parent / "bundled_fixture"
    bundled_plugin_dir = bundled_fixture / "fnack.spotiflac"
    bundled_plugin_dir.mkdir(parents=True, exist_ok=True)
    (bundled_plugin_dir / "plugin.json").write_text(
        '{"id":"fnack.spotiflac","name":"SpotiFLAC","version":"1.0.0",'
        '"type":["downloader"],"api_version":"^1.0","min_core_version":"0.2.0",'
        '"entry_point":"plugin:SpotiFLACDownloader","author":"fnack",'
        '"description":"test bundled","permissions":["network"],'
        '"settings_schema":[],"ui":{"slots":[]},"dependencies":{},"trust_level":"official"}'
    )
    (bundled_plugin_dir / "plugin.py").write_text(
        "from plugins.base import DownloaderPlugin, DownloadResult, TrackRef\n"
        "class SpotiFLACDownloader(DownloaderPlugin):\n"
        "    priority = 10\n"
        "    def can_handle(self, track: TrackRef) -> bool: return True\n"
        "    def download(self, track, dest_dir, options): return DownloadResult(success=True)\n"
    )

    # Brief 6 §4 fixture: a bundled plugin whose min_core_version is far above
    # the running core — it must FAIL to load but still appear in the list
    # with a load_error (Unsupported state), not vanish silently.
    bad_plugin_dir = bundled_fixture / "fnack.requires-newer-core"
    bad_plugin_dir.mkdir(parents=True, exist_ok=True)
    (bad_plugin_dir / "plugin.json").write_text(
        '{"id":"fnack.requires-newer-core","name":"Needs Newer Core","version":"1.0.0",'
        '"type":["event_hook"],"api_version":"^1.0","min_core_version":"99.0.0",'
        '"entry_point":"plugin:NeedsNewerCore","author":"fnack",'
        '"description":"test mismatch","permissions":[],'
        '"settings_schema":[],"ui":{"slots":[]},"dependencies":{},"trust_level":"official"}'
    )
    (bad_plugin_dir / "plugin.py").write_text(
        "from plugins.base import EventHookPlugin\n"
        "class NeedsNewerCore(EventHookPlugin): pass\n"
    )

    manager = init_plugin_manager(
        plugins_dir=str(Path(__file__).resolve().parent.parent / "examples" / "plugins"),
        bundled_plugins_dir=str(bundled_fixture),
        core_version=__version__,
    )
    manager.load_all()  # no enabled_ids filter -> enable everything discovered
    loaded_list = manager.list_loaded()
    print("Loaded plugins:", loaded_list)
    loaded_map = {p["id"]: p for p in loaded_list}
    assert "dev.fnack.example-quality-flag" in loaded_map, (
        "expected the bundled example plugin to be discovered"
    )
    assert loaded_map["dev.fnack.example-quality-flag"]["enabled"] is True, "plugin should be enabled"

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
    from plugins.models import InstalledPlugin
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
    loaded_map = {p["id"]: p for p in manager.list_loaded()}
    assert loaded_map["dev.fnack.example-quality-flag"]["enabled"] is False

    # Phase 1: enable creates the missing InstalledPlugin row (persists across
    # restart) — the live finding from the Phase 0 validation run.
    r = client.post("/api/plugins/dev.fnack.example-quality-flag/enable")
    print("Enable (row creation):", r.status_code, r.json)
    assert r.status_code == 200
    row = db.session.get(InstalledPlugin, "dev.fnack.example-quality-flag")
    assert row is not None and row.enabled is True, "enable must persist an InstalledPlugin row"

    # Phase 1: priority override endpoint + grouped listing
    r = client.post("/api/plugins/dev.fnack.example-quality-flag/priority", json={"priority": 7})
    print("Priority POST:", r.status_code, r.json)
    assert r.status_code == 200 and r.json.get("priority") == 7
    loaded_map = {p["id"]: p for p in manager.list_loaded()}
    assert loaded_map["dev.fnack.example-quality-flag"]["priority_override"] == 7

    r = client.post("/api/plugins/dev.fnack.example-quality-flag/priority", json={"priority": None})
    assert r.status_code == 200 and r.json.get("priority") is None

    r = client.get("/api/plugins/grouped")
    print("Grouped:", r.status_code, list((r.json or {}).keys()))
    assert r.status_code == 200 and "event_hook" in (r.json or {})
    assert "ui_extension" in (r.json or {})

    # Brief 5 §4: the loaded-plugin listing exposes the manifest description.
    listed = manager.list_loaded()
    assert "description" in listed[0], "list_loaded() must expose description"
    assert isinstance(listed[0]["description"], str)

    # Brief 6 §2: actions field surfaces (and defaults to []).
    assert "actions" in listed[0], "list_loaded() must expose actions"

    # Brief 6 §4: the version-mismatch fixture appears with a load_error
    # (Unsupported), NOT silently vanished.
    by_id = {p["id"]: p for p in manager.list_loaded()}
    assert "fnack.requires-newer-core" in by_id, \
        "version-mismatch plugin must still appear in the list"
    assert by_id["fnack.requires-newer-core"]["load_error"], \
        "version-mismatch plugin must carry a load_error reason"
    assert "requires fnack" in by_id["fnack.requires-newer-core"]["load_error"]

    # Brief 6 §3: updating a bundled plugin is refused (they update with the
    # fnack image).
    r = client.post("/api/plugins/fnack.spotiflac/update")
    assert r.status_code == 400

    # Phase 3/4: bundled install/uninstall guards. Installing an ACTIVE
    # bundled id from a repo is refused; uninstalling a bundled id is now
    # ALLOWED (records a tombstone so auto-install won't resurrect it).
    r = client.post("/api/plugins/install", json={"plugin_id": "fnack.spotiflac"})
    print("Install active bundled (refused):", r.status_code, r.json)
    assert r.status_code == 400 and "bundled" in (r.json.get("error") or "").lower()

    r = client.post("/api/plugins/fnack.spotiflac/uninstall")
    print("Uninstall bundled (allowed, tombstones):", r.status_code, r.json)
    assert r.status_code == 200

    # After uninstall, the tombstone exists (auto-install will skip it).
    from models import AppSetting
    assert db.session.get(AppSetting, "plugin.uninstalled.fnack.spotiflac") is not None, \
        "uninstall must record a tombstone for bundled plugins"

    # Reinstall after uninstall is permitted (no crash; 200 if a repo exists
    # for it, 400 otherwise).
    r = client.post("/api/plugins/install", json={"plugin_id": "fnack.spotiflac"})
    print("Reinstall bundled after uninstall:", r.status_code, r.json)
    assert r.status_code in (200, 400)

    # Phase 3: marketplace/repositories endpoints exist and are well-formed.
    r = client.get("/api/plugins/marketplace")
    print("Marketplace:", r.status_code, r.json)
    assert r.status_code == 200 and isinstance(r.json, list)

    r = client.get("/api/plugins/repositories")
    print("Repositories:", r.status_code, r.json)
    assert r.status_code == 200 and isinstance(r.json, list)

print("\nSMOKE TEST PASSED")
