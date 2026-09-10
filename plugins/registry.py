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


def _safe_extract(zf: zipfile.ZipFile, dest_dir: Path) -> None:
    """Extract an archive with a resolved-path containment check.

    Every member's resolved target must stay inside ``dest_dir`` — a crafted
    archive with ../../ entries (zip-slip) must never write outside the
    plugin directory. Raises RegistryError before anything is extracted when
    any member escapes."""
    dest_root = dest_dir.resolve()
    for member in zf.infolist():
        target = (dest_root / member.filename).resolve()
        if target != dest_root and dest_root not in target.parents:
            raise RegistryError(f"unsafe path in plugin archive: {member.filename!r}")
    zf.extractall(dest_dir)


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
        """Marketplace browse — ONE entry per (repository, plugin), no
        silent cross-repo de-duplication.

        Identity contract (see docs/architecture.md, "Marketplace identity"):
          * A plugin id names a plugin ON DISK; repositories are independent
            catalogs and may publish the same id with different content.
          * Browse shows every (repo, plugin) pair, each tagged with its
            source repository. Duplicates are surfaced, never resolved
            implicitly: every entry carries ``also_in_repos`` (the other
            enabled repositories publishing the same id) and
            ``installed_elsewhere`` when the id is already installed from a
            different repository.
          * Install/update always name their source repository
            (source_repo_id). An install WITHOUT one is refused when more
            than one enabled repository publishes the id — no implicit
            "first repo that has it" anywhere.

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
        installed_sources = {
            p.id: p.source_repo_id
            for p in InstalledPlugin.query.all()
            if p.id in present_ids and p.source_repo_id is not None
        }
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
        entries: list[dict] = []
        by_id: dict[str, list[dict]] = {}
        for repo in PluginRepository.query.filter_by(enabled=True).all():
            if not repo.cached_index_json:
                continue
            try:
                index = json.loads(repo.cached_index_json)
            except Exception:
                continue
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
                entry["installed_from_repo_id"] = installed_sources.get(entry.get("id"))
                entry["installed_elsewhere"] = bool(
                    entry["installed_from_repo_id"]
                    and entry["installed_from_repo_id"] != repo.id
                )
                by_id.setdefault(entry["id"], []).append(entry)
                entries.append(entry)
        # Duplicate detection across enabled repositories: every entry lists
        # the OTHER repositories publishing the same id (warning surface).
        for e in entries:
            others = [o for o in by_id.get(e["id"], [])
                      if o is not e and o["source_repo_id"] != e["source_repo_id"]]
            e["also_in_repos"] = [{
                "repo_id": o["source_repo_id"],
                "repo_name": o["source_repo_name"],
                "latest_version": o.get("latest_version"),
            } for o in others]
        return entries

    def _repos_publishing(self, plugin_id: str) -> list[dict]:
        """Every enabled repository whose cached index publishes ``plugin_id``
        as [{repo_id, repo_name, entry}]. One per repository."""
        out: list[dict] = []
        for repo in PluginRepository.query.filter_by(enabled=True).all():
            if not repo.cached_index_json:
                continue
            try:
                index = json.loads(repo.cached_index_json)
            except Exception:
                continue
            for entry in index.get("plugins", []):
                if entry.get("id") == plugin_id:
                    out.append({
                        "repo_id": repo.id,
                        "repo_name": repo.name,
                        "entry": entry,
                    })
                    break
        return out

    def repository_conflicts(self, repo_id: int) -> list[dict]:
        """Duplicate-id warnings for one repository: every plugin id it
        publishes that ANOTHER enabled repository also publishes. Returns
        [{plugin_id, other_repos: [names]}] — empty when no duplicates."""
        repo = db.session.get(PluginRepository, repo_id)
        if not repo or not repo.cached_index_json:
            return []
        try:
            mine_ids = {e.get("id") for e in (json.loads(repo.cached_index_json) or {}).get("plugins", []) if e.get("id")}
        except Exception:
            return []
        others = [
            o for o in PluginRepository.query.filter(
                PluginRepository.enabled.is_(True),
                PluginRepository.id != repo_id).all()
            if o.cached_index_json
        ]
        other_ids: dict[str, set] = {}
        for o in others:
            try:
                oids = {e.get("id") for e in (json.loads(o.cached_index_json) or {}).get("plugins", []) if e.get("id")}
            except Exception:
                continue
            for pid in oids:
                other_ids.setdefault(pid, set()).add(o.name)
        return [
            {"plugin_id": pid, "other_repos": sorted(names)}
            for pid, names in other_ids.items() if pid in mine_ids
        ]

    # -- install / update / uninstall ---------------------------------------

    def install(self, plugin_id: str, version: Optional[str] = None,
                source_repo_id: Optional[int] = None) -> InstalledPlugin:
        """Install ``plugin_id`` FROM AN EXPLICIT SOURCE REPOSITORY.

        ``source_repo_id`` is provenance, not a hint: when given, the id must
        be published by that repository or the install is refused. When
        omitted, exactly one enabled repository may publish the id — more
        than one is REFUSED as ambiguous (candidates are listed) rather than
        silently resolving to whichever repo was added first."""
        repos = self._repos_publishing(plugin_id)
        if not repos:
            raise RegistryError(f"{plugin_id} not found in any added repository")
        if source_repo_id is not None:
            chosen = next((r for r in repos if r["repo_id"] == source_repo_id), None)
            if chosen is None:
                names = ", ".join(f"{r['repo_name']} (id {r['repo_id']})" for r in repos)
                raise RegistryError(
                    f"{plugin_id} is not published by repository {source_repo_id} — "
                    f"it is published by: {names}"
                )
        elif len(repos) == 1:
            chosen = repos[0]
        else:
            names = ", ".join(
                f"{r['repo_name']} (id {r['repo_id']}, v{r['entry'].get('latest_version')})"
                for r in repos
            )
            raise RegistryError(
                f"'{plugin_id}' is published by more than one enabled repository: "
                f"{names}. Installs are repo-scoped — pass the source_repo_id "
                f"of the repository you want to install from."
            )
        entry = chosen["entry"]
        repo_id = chosen["repo_id"]
        version = version or entry["latest_version"]
        version_info = entry.get("versions", {}).get(version)
        if not version_info:
            raise RegistryError(
                f"{plugin_id} {version} is not published by repository "
                f"'{chosen['repo_name']}' (has: {sorted((entry.get('versions') or {}).keys())})"
            )

        # FAIL CLOSED: every install must be checksummed. A repository index
        # entry that omits sha256 is refused — no unchecked code path exists.
        expected_sha = version_info.get("sha256")
        if not expected_sha:
            raise RegistryError(
                f"{plugin_id} {version} has no published sha256 in the repository "
                f"index — refusing to install (indexes must checksum every release)"
            )

        archive_bytes = self._download(version_info["download_url"])
        if hashlib.sha256(archive_bytes).hexdigest() != expected_sha:
            raise RegistryError(f"checksum mismatch for {plugin_id} {version} — refusing to install")

        dest_dir = self.manager.plugins_dir / plugin_id
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            _safe_extract(zf, dest_dir)  # zip-slip containment check

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
        row.source_repo_id = repo_id  # provenance recorded for future updates
        row.manifest_json = json.dumps(entry)
        row.enabled = True
        db.session.commit()

        self.manager.enable_plugin(plugin_id)
        return row

    def update(self, plugin_id: str) -> InstalledPlugin:
        """Update an installed plugin from the repository it was INSTALLED
        from (row.source_repo_id) — never from whichever repo happens to be
        queried first. Settings (PluginSetting rows) are keyed by plugin_id,
        independent of InstalledPlugin, so they survive this re-install
        untouched."""
        row = db.session.get(InstalledPlugin, plugin_id)
        if row is None or row.source_repo_id is None:
            raise RegistryError(
                f"{plugin_id} has no recorded source repository — only "
                f"marketplace-installed plugins can update independently"
            )
        repo = db.session.get(PluginRepository, row.source_repo_id)
        if repo is None or not repo.enabled or not repo.cached_index_json:
            raise RegistryError(
                f"the source repository of {plugin_id} is gone or disabled — "
                f"re-add/refresh it, then update"
            )
        return self.install(plugin_id, version=None, source_repo_id=row.source_repo_id)

    def uninstall(self, plugin_id: str) -> None:
        self.manager.unload_plugin(plugin_id)
        row = db.session.get(InstalledPlugin, plugin_id)
        if row:
            db.session.delete(row)
            db.session.commit()
        dest_dir = self.manager.plugins_dir / plugin_id
        if dest_dir.exists():
            shutil.rmtree(dest_dir)

    def latest_versions(self) -> dict[str, str]:
        """{plugin_id: latest_version} for update flags — SOURCE-AWARE: each
        installed plugin is checked against the repository it was installed
        from, so a duplicate id in another repository never drives the
        "update available" flag for an install that did not come from it."""
        out: dict[str, str] = {}
        for row in InstalledPlugin.query.filter(
                InstalledPlugin.source_repo_id.isnot(None)).all():
            repo = db.session.get(PluginRepository, row.source_repo_id)
            if not repo or not repo.enabled or not repo.cached_index_json:
                continue  # source repo gone — no update signal
            try:
                index = json.loads(repo.cached_index_json)
            except Exception:
                continue
            for entry in index.get("plugins", []):
                if entry.get("id") == row.id:
                    lv = entry.get("latest_version")
                    if lv:
                        out[row.id] = lv
                    break
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
