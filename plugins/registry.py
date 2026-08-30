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
        (bundled wins — never installable from a repo)."""
        installed_ids = {p.id: p.version for p in InstalledPlugin.query.all()}
        bundled_ids = set()
        try:
            bundled_ids = {d.name for d in self.manager.discover_bundled()}
        except Exception:
            pass
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
                entry["bundled"] = entry.get("id") in bundled_ids
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

    def _download(self, url: str) -> bytes:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.content
