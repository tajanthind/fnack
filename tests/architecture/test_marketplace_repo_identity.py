"""Architecture tests: multi-repository plugin-id contract (repo-scoped).

The Marketplace identity contract (docs/architecture.md, "Marketplace
identity"):

1. Browse is per (repository, plugin) — duplicate ids across enabled
   repositories are NEVER merged or "newest-wins"; every entry names its
   source repo and lists the other publishers (`also_in_repos`).
2. Install is repo-scoped: `install(plugin_id, version, source_repo_id)`.
   Without a source, an id published by >1 enabled repository is REFUSED as
   ambiguous (candidates listed) — no implicit "first repo that has it".
3. The chosen source is recorded (`InstalledPlugin.source_repo_id`) and
   `update()` re-installs from THAT repository only.
4. Installing from a repo that does not publish the id/version is refused.
5. Adding a repository that collides with an enabled one is detected
   (`repository_conflicts`).

Run from the repo root:

    .venv/bin/python tests/architecture/test_marketplace_repo_identity.py
"""

import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _index(name: str, plugins: list[dict]) -> dict:
    return {"name": name, "plugins": plugins}


def _entry(plugin_id: str, version: str, url: str) -> dict:
    """A minimal index entry (registry only needs id/latest_version/versions
    with sha256 + download_url for the target version)."""
    return {
        "id": plugin_id,
        "name": plugin_id.replace(".", " ").title(),
        "latest_version": version,
        "versions": {
            version: {
                "sha256": "0" * 64,  # real content hashing happens below
                "download_url": url,
                "min_core_version": "0.2.0",
                "api_version": "^1.0",
            }
        },
        "capabilities": [],
        "permissions": [],
        "trust_level": "community",
    }


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _benign_zip(plugin_id: str, version: str) -> bytes:
    manifest = {
        "id": plugin_id, "name": "Test " + plugin_id, "version": version,
        "type": ["library_task"], "api_version": "^1.0",
        "entry_point": "plugin:Noop", "permissions": [],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("plugin.json", json.dumps(manifest))
        zf.writestr("plugin.py", "# noop\nclass Noop:\n    pass\n")
    return buf.getvalue()


class _StubManager:
    """Minimal PluginManager stand-in for registry tests: only the surface
    PluginRegistry touches, backed by a temp plugins dir."""

    def __init__(self, root: Path):
        self.plugins_dir = root / "plugins"
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.enabled_calls: list[str] = []
        self.loaded_manifests: dict[str, dict] = {}

    def discover(self):
        out = []
        for d in sorted(self.plugins_dir.iterdir()):
            if d.is_dir() and (d / "plugin.json").exists():
                out.append(d)
        return out

    def discover_bundled(self):
        return set()

    def load_plugin(self, dest_dir: Path):
        manifest = json.loads((Path(dest_dir) / "plugin.json").read_text(encoding="utf-8"))
        self.loaded_manifests[manifest["id"]] = manifest
        return SimpleNamespace(manifest=SimpleNamespace(
            id=manifest["id"],
            name=manifest["name"],
            version=manifest["version"],
            type=manifest["type"],
            trust_level=manifest.get("trust_level", "community"),
        ))

    def enable_plugin(self, plugin_id: str) -> None:
        self.enabled_calls.append(plugin_id)


_DB_COUNTER = {"n": 0}


def _make_app():
    from flask import Flask
    from models import db
    _DB_COUNTER["n"] += 1
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///file:fnack_repo_identity_{_DB_COUNTER['n']}"
        "?mode=memory&cache=shared&uri=true")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app, db


def _seed_repos(db, tmp: Path, monkey_download) -> dict:
    """Two enabled repos sharing the id 'com.example.dup' (A has v2.0.0,
    B has v1.0.0), each with one unique plugin. Returns {repo_a_id, repo_b_id}."""
    from plugins.models import PluginRepository

    dup_a_zip = _benign_zip("com.example.dup", "2.0.0")
    dup_b_zip = _benign_zip("com.example.dup", "1.0.0")
    only_a_zip = _benign_zip("com.example.onlyA", "1.0.0")
    monkey_download.setdefault("https://a/dup.zip", dup_a_zip)
    monkey_download.setdefault("https://b/dup.zip", dup_b_zip)
    monkey_download.setdefault("https://a/onlyA.zip", only_a_zip)

    dup_a = _entry("com.example.dup", "2.0.0", "https://a/dup.zip")
    dup_b = _entry("com.example.dup", "1.0.0", "https://b/dup.zip")
    only_a = _entry("com.example.onlyA", "1.0.0", "https://a/onlyA.zip")
    only_b = _entry("com.example.onlyB", "1.0.0", "https://b/onlyB.zip")
    # Real content hashes so the registry's fail-closed checksum passes.
    for entry, blob in [(dup_a, dup_a_zip), (dup_b, dup_b_zip),
                        (only_a, only_a_zip), (only_b, _benign_zip("com.example.onlyB", "1.0.0"))]:
        ver = entry["versions"][entry["latest_version"]]
        ver["sha256"] = _sha256(blob)
    monkey_download.setdefault("https://b/onlyB.zip", _benign_zip("com.example.onlyB", "1.0.0"))

    idx_a = _index("Repo A", [dup_a, only_a])
    idx_b = _index("Repo B", [dup_b, only_b])
    a = PluginRepository(name="Repo A", url="https://a/index.json",
                         cached_index_json=json.dumps(idx_a), enabled=True)
    b = PluginRepository(name="Repo B", url="https://b/index.json",
                         cached_index_json=json.dumps(idx_b), enabled=True)
    db.session.add_all([a, b])
    db.session.commit()
    return {"a": a.id, "b": b.id}


def test_browse_is_per_repo_and_duplicates_are_surfaced() -> None:
    from plugins.models import PluginRepository
    app, db = _make_app()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CONFIG_DIR"] = tmp
        manager = _StubManager(Path(tmp))
        downloads: dict[str, bytes] = {}
        with app.app_context():
            import plugins.models  # noqa: F401
            db.create_all()
            ids = _seed_repos(db, Path(tmp), downloads)
            from plugins.registry import PluginRegistry
            registry = PluginRegistry(manager)
            entries = registry.list_available()
            # 4 entries: dup x2 + onlyA + onlyB — no cross-repo merging.
            assert len(entries) == 4, f"expected 4 per-repo entries, got {len(entries)}"
            by_id = {}
            for e in entries:
                by_id.setdefault(e["id"], []).append(e)
            assert len(by_id["com.example.dup"]) == 2, "duplicate id must appear once PER repo"
            for e in by_id["com.example.dup"]:
                assert e["also_in_repos"], "duplicate entry must list the other publisher"
                assert len(e["also_in_repos"]) == 1
                assert e["also_in_repos"][0]["repo_id"] != e["source_repo_id"]
            assert not by_id["com.example.onlyA"][0]["also_in_repos"]
            assert not by_id["com.example.onlyB"][0]["also_in_repos"]
            # repository_conflicts: adding B (already added here) reports the dup.
            conflicts = registry.repository_conflicts(ids["b"])
            assert [c["plugin_id"] for c in conflicts] == ["com.example.dup"]
            assert conflicts[0]["other_repos"] == ["Repo A"]
            del os.environ["CONFIG_DIR"]


def test_ambiguous_install_without_source_is_refused() -> None:
    app, db = _make_app()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CONFIG_DIR"] = tmp
        manager = _StubManager(Path(tmp))
        downloads: dict[str, bytes] = {}
        with app.app_context():
            import plugins.models  # noqa: F401
            db.create_all()
            _seed_repos(db, Path(tmp), downloads)
            from plugins.registry import PluginRegistry, RegistryError
            registry = PluginRegistry(manager)
            try:
                registry.install("com.example.dup")
                raise AssertionError("ambiguous install must be refused")
            except RegistryError as e:
                assert "more than one enabled repository" in str(e)
                assert "Repo A" in str(e) and "Repo B" in str(e)
            # A unique id installs fine without a source (only one publisher).
            def _download(url):
                body = _benign_zip("com.example.onlyA", "1.0.0")
                downloads[url] = body
                return body
            registry._download = _download
            row = registry.install("com.example.onlyA")
            assert row.source_repo_id == 1
            del os.environ["CONFIG_DIR"]


def test_install_takes_explicit_source_and_records_provenance() -> None:
    from plugins.models import InstalledPlugin
    app, db = _make_app()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CONFIG_DIR"] = tmp
        manager = _StubManager(Path(tmp))
        downloads: dict[str, bytes] = {}
        with app.app_context():
            import plugins.models  # noqa: F401
            db.create_all()
            ids = _seed_repos(db, Path(tmp), downloads)
            from plugins.registry import PluginRegistry, RegistryError
            registry = PluginRegistry(manager)

            def _download(url):
                # the benign zip's manifest must carry the requested version
                return downloads.get(url, _benign_zip("com.example.dup", "2.0.0"))

            registry._download = _download
            # Source repo A, version 2.0.0 -> downloads from A's URL.
            row = registry.install("com.example.dup", "2.0.0", source_repo_id=ids["a"])
            assert row.source_repo_id == ids["a"]
            assert manager.loaded_manifests["com.example.dup"]["version"] == "2.0.0"
            # Refuse a version the chosen repo does not publish.
            try:
                registry.install("com.example.dup", "2.0.0", source_repo_id=ids["b"])
                raise AssertionError("must refuse a version the source repo lacks")
            except RegistryError as e:
                assert "Repo B" in str(e)
            # Refuse a plugin id the chosen repo does not publish at all.
            try:
                registry.install("com.example.onlyB", "1.0.0", source_repo_id=ids["a"])
                raise AssertionError("must refuse an id the source repo lacks")
            except RegistryError as e:
                assert "not published by repository" in str(e)
            # Provenance recorded -> update() re-installs from repo A, and the
            # other repo's listing is flagged installed_elsewhere.
            url_hits = []
            original = registry._download

            def _tracking(url):
                url_hits.append(url)
                return original(url)

            registry._download = _tracking
            row2 = registry.update("com.example.dup")
            assert row2.source_repo_id == ids["a"]
            assert url_hits, "update must download from the source repo"
            assert all("a/dup.zip" in u for u in url_hits), url_hits

            present = {d.name for d in manager.discover()}
            assert "com.example.dup" in present
            for e in registry.list_available():
                if e["id"] == "com.example.dup" and e["source_repo_id"] == ids["b"]:
                    assert e["installed_elsewhere"] is True
                    assert e["installed_from_repo_id"] == ids["a"]
            assert db.session.get(InstalledPlugin, "com.example.dup") is not None
            del os.environ["CONFIG_DIR"]


if __name__ == "__main__":
    test_browse_is_per_repo_and_duplicates_are_surfaced()
    test_ambiguous_install_without_source_is_refused()
    test_install_takes_explicit_source_and_records_provenance()
    print("test_marketplace_repo_identity: PASSED")
