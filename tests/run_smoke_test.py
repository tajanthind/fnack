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

    # Brief 7 §4 fixture: a multi-type plugin (library_source + server_extension)
    # shaped like the bundled fnack.lidarr — the extraction of the old
    # services/lidarr_service.py. Verifies the class can implement both
    # interfaces and that its register_routes() blueprint actually serves.
    lidarr_dir = bundled_fixture / "fnack.lidarr"
    lidarr_dir.mkdir(parents=True, exist_ok=True)
    (lidarr_dir / "plugin.json").write_text(
        '{"id":"fnack.lidarr","name":"Lidarr Test","version":"1.0.0",'
        '"type":["library_source","server_extension"],"api_version":"^1.0",'
        '"min_core_version":"0.2.0","entry_point":"plugin:LidarrTestPlugin",'
        '"author":"fnack","description":"test lidarr","permissions":["settings"],'
        '"settings_schema":[{"key":"api_key","type":"secret","default":""}],'
        '"ui":{"slots":[]},"dependencies":{},"trust_level":"official"}'
    )
    (lidarr_dir / "plugin.py").write_text(
        "from flask import Blueprint, jsonify\n"
        "from plugins.base import LibrarySourcePlugin, ServerExtensionPlugin\n"
        "class LidarrTestPlugin(LibrarySourcePlugin, ServerExtensionPlugin):\n"
        "    def list_artists(self): return []\n"
        "    def register_routes(self, blueprint: Blueprint) -> None:\n"
        "        @blueprint.route('/api/sabnzbd-test', methods=['GET'])\n"
        "        def sabnzbd_test():\n"
        "            return jsonify({'ok': True, 'plugin': 'fnack.lidarr'})\n"
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

    # Brief 7 §4: register server_extension plugin blueprints BEFORE the
    # first request (same loop app.py runs for enabled ServerExtensionPlugins,
    # incl. the bundled fnack.lidarr). The multi-type lidarr fixture must
    # serve its register_routes() blueprint.
    from plugins.base import ServerExtensionPlugin
    from flask import Blueprint as _FlaskBlueprint
    for _loaded in manager._plugins.values():  # noqa: SLF001 - mirrors app.py
        if not _loaded.enabled or not isinstance(_loaded.instance, ServerExtensionPlugin):
            continue
        _bp = _FlaskBlueprint(f"smoke_{_loaded.manifest.id.replace('.', '_').replace('-', '_')}",
                              __name__, url_prefix="")
        _loaded.instance.register_routes(_bp)
        app.register_blueprint(_bp)

    client = app.test_client()
    r = client.get("/api/sabnzbd-test")
    print("Lidarr fixture route:", r.status_code, r.json)
    assert r.status_code == 200 and r.json.get("plugin") == "fnack.lidarr"
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

    # Phase 1 (MASTER): capabilities field surfaces on the loaded listing,
    # and the capability registry is populated from loaded plugins.
    assert "capabilities" in listed[0], "list_loaded() must expose capabilities"

    # Brief 6 §4: the version-mismatch fixture appears with a load_error
    # (Unsupported), NOT silently vanished.
    by_id = {p["id"]: p for p in manager.list_loaded()}
    assert "fnack.requires-newer-core" in by_id, \
        "version-mismatch plugin must still appear in the list"
    assert by_id["fnack.requires-newer-core"]["load_error"], \
        "version-mismatch plugin must carry a load_error reason"
    assert "requires fnack" in by_id["fnack.requires-newer-core"]["load_error"]

    # Phase 1 (MASTER): capability registry + public manager API.
    spotiflac_caps = by_id["fnack.spotiflac"]["capabilities"]
    print("SpotiFLAC capabilities:", spotiflac_caps)
    assert "download.track" in spotiflac_caps
    assert manager.capability_registry.has("download.track"), \
        "capability registry must contain download.track from the enabled fixture"
    assert manager.get_plugin("fnack.spotiflac") is not None, "get_plugin() public API"
    assert manager.get_loaded("fnack.spotiflac") is not None, "get_loaded() public API"
    assert manager.get_plugin_context("fnack.spotiflac") is not None, "get_plugin_context() public API"
    assert manager.get_plugin_capabilities("fnack.spotiflac") == ["download.track"]

    # Phase 1.1: capability-specific priority — per-capability override,
    # capability registry ordering, API endpoints.
    assert by_id["fnack.spotiflac"]["capability_priorities"] == {"download.track": 10}, \
        "list_loaded must expose per-capability effective priorities"
    r = client.get("/api/plugins/fnack.spotiflac/capabilities")
    print("GET /capabilities:", r.status_code, r.json)
    assert r.status_code == 200
    caps = {c["capability_id"]: c for c in r.json["capabilities"]}
    assert caps["download.track"]["priority"] == 10
    assert caps["download.track"]["source"] in ("manifest", "plugin")
    r = client.post("/api/plugins/fnack.spotiflac/capabilities/download.track/priority",
                    json={"priority": 3})
    print("POST capability priority:", r.status_code, r.json)
    assert r.status_code == 200 and r.json["capability_priorities"]["download.track"] == 3
    assert manager.capability_registry.priority_for("fnack.spotiflac", "download.track") == 3
    # Clear restores the plugin-level default.
    r = client.post("/api/plugins/fnack.spotiflac/capabilities/download.track/priority",
                    json={"priority": None})
    assert r.status_code == 200 and r.json["capability_priorities"]["download.track"] == 10
    # Unknown capability rejected.
    r = client.post("/api/plugins/fnack.spotiflac/capabilities/not.a.cap/priority",
                    json={"priority": 5})
    assert r.status_code == 400
    # Phase 1.1 §7: non-integer priority -> clean 400, no unhandled ValueError.
    r = client.post("/api/plugins/fnack.spotiflac/capabilities/download.track/priority",
                    json={"priority": "banana"})
    assert r.status_code == 400 and "integer" in r.json.get("error", "")
    r = client.post("/api/plugins/fnack.spotiflac/priority", json={"priority": "banana"})
    assert r.status_code == 400 and "integer" in r.json.get("error", "")

    # Phase 1.1 §6: config export carries capability_priorities; import
    # restores them. (Ensure an InstalledPlugin row exists so the export
    # includes the plugin — the enable endpoint creates the row.)
    r = client.post("/api/plugins/fnack.spotiflac/enable")
    assert r.status_code == 200
    r = client.post("/api/plugins/fnack.spotiflac/capabilities/download.track/priority",
                    json={"priority": 7})
    assert r.status_code == 200
    r = client.get("/api/plugins/export")
    print("Export status:", r.status_code)
    assert r.status_code == 200
    exported = r.json
    assert "fnack.spotiflac" in exported["plugins"]
    assert exported["plugins"]["fnack.spotiflac"]["capability_priorities"] == {"download.track": 7}, \
        "export must include capability_priorities"
    # Import a modified blob — the endpoint must accept capability_priorities
    # and restore them for plugins that install successfully (same condition
    # as settings restore; the fixture has no repo so install is skipped, but
    # the handler must not choke on the field).
    blob = exported
    blob["plugins"]["fnack.spotiflac"]["capability_priorities"] = {"download.track": 11}
    r = client.post("/api/plugins/import", json=blob)
    print("Import status:", r.status_code)
    assert r.status_code == 200

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

    # Brief 7 §4: the multi-type (library_source + server_extension) lidarr
    # fixture is listed under both types (routes verified above, before the
    # first request was handled).
    by_id = {p["id"]: p for p in manager.list_loaded()}
    assert "fnack.lidarr" in by_id, "multi-type lidarr fixture must load"
    lidarr_manifest = by_id["fnack.lidarr"]
    assert "library_source" in lidarr_manifest["type"], \
        "lidarr fixture must be listed as library_source"
    assert "server_extension" in lidarr_manifest["type"], \
        "lidarr fixture must be listed as server_extension"

print("\nSMOKE TEST PASSED")
