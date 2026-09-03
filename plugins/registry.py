"""Repository & marketplace logic: add a repo URL, cache its index, browse
available plugins, install/update/uninstall.

A "repository" is nothing more than a URL to a JSON index (see
PLUGIN_ARCHITECTURE.md §5 for the exact shape). This module never executes
anything from a repo except after an explicit user-triggered install, and
even then it goes through PluginManager.load_plugin(), which validates the
manifest before importing any code.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from models import db
from plugins.manager import PluginManager
from plugins.models import InstalledPlugin, PluginRepository

logger = logging.getLogger("fnack.plugins.registry")

REQUEST_TIMEOUT = 15


class RegistryError(Exception):
    pass


class PluginRegistry:
    def __init__(self, manager: PluginManager):
        self.manager = manager

    # -- repositories -----------------------------------------------------

    def add_repository(self, url: str) -> PluginRepository:
        index = self._fetch_index(url)
        repo = PluginRepository(
            name=index.get("name", url),
            url=url,
            cached_index_json=json.dumps(index),
            last_synced_at=datetime.now(timezone.utc),
        )
        db.session.add(repo)
        db.session.commit()
        return repo

    def refresh_repository(self, repo_id: int) -> PluginRepository:
        repo = db.session.get(PluginRepository, repo_id)
        if not repo:
            raise RegistryError(f"no such repository: {repo_id}")
        index = self._fetch_index(repo.url)
        repo.cached_index_json = json.dumps(index)
        repo.last_synced_at = datetime.now(timezone.utc)
        db.session.commit()
        return repo

    def remove_repository(self, repo_id: int) -> None:
        repo = db.session.get(PluginRepository, repo_id)
        if repo:
            db.session.delete(repo)
            db.session.commit()

    def _fetch_index(self, url: str) -> dict:
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            index = resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            raise RegistryError(f"could not fetch/parse repository index: {exc}") from exc
        if "plugins" not in index or not isinstance(index["plugins"], list):
            raise RegistryError("repository index missing a 'plugins' array")
        return index

    # -- browsing -----------------------------------------------------------

    def list_available(self) -> list[dict]:
        """Merged, de-duplicated (by id, newest wins) plugin listing across
        every enabled repository's cached index, annotated with whether
        it's already installed and whether the id is a bundled plugin
        (bundled wins — never installable from a repo).

        "Installed" is computed from what is PHYSICALLY present (a bundled
        dir in the image or a dir under the plugins dir), not from orphaned
        InstalledPlugin rows: rows left over from plugins that used to ship
        with the image but no longer do must not make the Marketplace report
        a plugin as installed when its code is not actually there."""
        present_ids: set[str] = set()
        try:
            for d in self.manager.discover():
                mf = d / "plugin.json"
                if not mf.exists():
                    continue
                try:
                    pid = json.loads(mf.read_text(encoding="utf-8")).get("id")
                except Exception:
                    pid = None
                present_ids.add(pid or d.name)
        except Exception:
            pass
        installed_ids = {p.id: p.version for p in InstalledPlugin.query.all()
                         if p.id in present_ids}
        bundled_ids = set()
        try:
            bundled_ids = {d.name for d in self.manager.discover_bundled()}
        except Exception:
            pass
        # A bundled plugin the user uninstalled is marked with a tombstone —
        # it must NOT count as "bundled/installed" so the Marketplace can
        # reinstall it.
        from models import AppSetting, db
        try:
            tombstoned = {
                row.key.removeprefix("plugin.uninstalled.")
                for row in AppSetting.query.filter(AppSetting.key.like("plugin.uninstalled.%")).all()
            }
        except Exception:
            tombstoned = set()
        merged: dict[str, dict] = {}
        for repo in PluginRepository.query.filter_by(enabled=True).all():
            if not repo.cached_index_json:
                continue
            index = json.loads(repo.cached_index_json)
            for entry in index.get("plugins", []):
                entry = dict(entry)
                entry["source_repo_id"] = repo.id
                entry["source_repo_name"] = repo.name
                entry["installed_version"] = installed_ids.get(entry.get("id"))
                entry["bundled"] = entry.get("id") in bundled_ids and entry.get("id") not in tombstoned
                # Brief 6 §4: annotate compat so the Marketplace can grey out
                # incompatible plugins with a reason (min_core_version /
                # api_version from the entry's version payload, if present).
                vinfo = (entry.get("versions") or {}).get(entry.get("latest_version")) or {}
                entry["min_core_version"] = vinfo.get("min_core_version") or entry.get("min_core_version")
                entry["api_version"] = vinfo.get("api_version") or entry.get("api_version")
                # Phase 1: surface declared capabilities in the Marketplace.
                caps = entry.get("capabilities") or []
                if isinstance(caps, str):
                    caps = [caps]
                entry["capabilities"] = [str(c) for c in caps]
                merged[entry["id"]] = entry
        return list(merged.values())

    # -- install / update / uninstall ---------------------------------------

    def install(self, plugin_id: str, version: Optional[str] = None) -> InstalledPlugin:
        entry, repo_id = self._find_entry(plugin_id)
        version = version or entry["latest_version"]
        version_info = entry.get("versions", {}).get(version)
        if not version_info:
            raise RegistryError(f"{plugin_id} has no published version {version}")

        archive_bytes = self._download(version_info["download_url"])
        expected_sha = version_info.get("sha256")
        if expected_sha and hashlib.sha256(archive_bytes).hexdigest() != expected_sha:
            raise RegistryError(f"checksum mismatch for {plugin_id} {version} — refusing to install")

        dest_dir = self.manager.plugins_dir / plugin_id
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            zf.extractall(dest_dir)

        loaded = self.manager.load_plugin(dest_dir)  # validates manifest + imports code
        manifest = loaded.manifest

        row = db.session.get(InstalledPlugin, plugin_id)
        if row is None:
            row = InstalledPlugin(id=plugin_id)
            db.session.add(row)
        row.name = manifest.name
        row.version = manifest.version
        row.type = ",".join(manifest.type)
        row.trust_level = manifest.trust_level
        row.source_repo_id = repo_id
        row.manifest_json = json.dumps(entry)
        row.enabled = True
        db.session.commit()

        self.manager.enable_plugin(plugin_id)
        return row

    def update(self, plugin_id: str) -> InstalledPlugin:
        # Settings (PluginSetting rows) are keyed by plugin_id, independent
        # of InstalledPlugin, so they survive this re-install untouched.
        return self.install(plugin_id, version=None)

    def uninstall(self, plugin_id: str) -> None:
        self.manager.unload_plugin(plugin_id)
        row = db.session.get(InstalledPlugin, plugin_id)
        if row:
            db.session.delete(row)
            db.session.commit()
        dest_dir = self.manager.plugins_dir / plugin_id
        if dest_dir.exists():
            shutil.rmtree(dest_dir)

    def _find_entry(self, plugin_id: str) -> tuple[dict, Optional[int]]:
        for repo in PluginRepository.query.filter_by(enabled=True).all():
            if not repo.cached_index_json:
                continue
            index = json.loads(repo.cached_index_json)
            for entry in index.get("plugins", []):
                if entry.get("id") == plugin_id:
                    return entry, repo.id
        raise RegistryError(f"{plugin_id} not found in any added repository")

    def latest_versions(self) -> dict[str, str]:
        """{plugin_id: latest_version} across every enabled repository's
        cached index (newest wins for duplicates). Used by the Installed
        list to flag update_available (Brief 6 §3)."""
        out: dict[str, str] = {}
        for repo in PluginRepository.query.filter_by(enabled=True).all():
            if not repo.cached_index_json:
                continue
            try:
                index = json.loads(repo.cached_index_json)
            except Exception:
                continue
            for entry in index.get("plugins", []):
                pid = entry.get("id")
                lv = entry.get("latest_version")
                if pid and lv:
                    out[pid] = lv
        return out

    def _download(self, url: str) -> bytes:
        """Fetch a plugin archive, converting HTTP/network failures into a
        clear RegistryError (surfaced to the user as a 400 message, not a
        generic 500). A 404 here usually means the repository's release
        assets have not been published yet."""
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            raise RegistryError(
                f"download failed (HTTP {status}) for {url} — the plugin's "
                f"release may not be published yet; refresh the repository and "
                f"try again"
            ) from exc
        except requests.RequestException as exc:
            raise RegistryError(f"network error downloading {url}: {exc}") from exc
        return resp.content
