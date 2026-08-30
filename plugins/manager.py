"""PluginManager: the runtime home for loaded plugin instances.

Responsibilities:
  * discover installed plugins under `plugins_dir` (default /config/plugins/<id>/)
  * validate each manifest (required fields, api_version/min_core_version compatibility)
  * dynamically import the plugin module and instantiate its entry-point class
  * run lifecycle hooks (on_load / on_enable / on_disable / on_unload)
  * keep type-specific registries (downloaders sorted by priority, etc.)
  * wrap every call into plugin code with a timeout + exception guard, and
    auto-disable a plugin after repeated failures

This is intentionally framework-only: it doesn't know what any specific
plugin does, only how to load/run/retire one safely.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from plugins import PLUGIN_API_VERSION
from plugins.base import PluginBase, PluginManifest
from plugins.context import PluginContext
from plugins.events import EventBus

logger = logging.getLogger("fnack.plugins.manager")

MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_HOOK_TIMEOUT = 10.0
# Downloads legitimately take minutes (network fetch + verify); the 10s
# default would kill a real download mid-flight. The queue passes this for
# `download` calls, matching the old 180s service-level timeouts.
DOWNLOAD_HOOK_TIMEOUT = 600.0


class PluginLoadError(Exception):
    pass


class LoadedPlugin:
    def __init__(self, instance: PluginBase, manifest: PluginManifest, module_path: Path):
        self.instance = instance
        self.manifest = manifest
        self.module_path = module_path
        self.enabled = False
        self.consecutive_failures = 0
        self.priority_override: Optional[int] = None  # user override, from InstalledPlugin.priority_override

    def refresh_from_db(self) -> None:
        """Pull persisted per-install state (priority_override) from the DB."""
        try:
            from plugins.models import InstalledPlugin
            from models import db
            row = db.session.get(InstalledPlugin, self.manifest.id)
            if row is not None:
                self.priority_override = row.priority_override
        except Exception:
            logger.debug("Could not refresh plugin state for %s", self.manifest.id, exc_info=True)


class PluginManager:
    def __init__(self, plugins_dir: str | Path = "/config/plugins",
                 bundled_plugins_dir: str | Path | None = None,
                 core_version: str = "0.0.0"):
        self.plugins_dir = Path(plugins_dir)
        self.bundled_plugins_dir = Path(bundled_plugins_dir) if bundled_plugins_dir else None
        self.core_version = core_version
        self.event_bus = EventBus()
        self.ui_slot_registry: dict[str, list] = {}
        self._plugins: dict[str, LoadedPlugin] = {}
        self._lock = threading.Lock()
        self._scheduler_hook = self._default_scheduler_hook
        # Ensure the install dir exists so manual installs (INTEGRATION.md §7)
        # and the registry have somewhere to write.
        try:
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.debug("Could not create plugins dir %s", self.plugins_dir)

    # -- discovery & loading -------------------------------------------------

    def _plugin_dirs(self, root: Path) -> list[Path]:
        if not root.exists():
            return []
        return [p for p in root.iterdir() if p.is_dir() and (p / "plugin.json").exists()]

    def discover(self) -> list[Path]:
        """All plugin directories: bundled (image-baked, official) first, then
        user-installed under /config/plugins. Bundled dirs never shadow a
        user install of the same id — the user dir wins (later in the list)."""
        dirs: list[Path] = []
        if self.bundled_plugins_dir is not None:
            dirs.extend(self._plugin_dirs(self.bundled_plugins_dir))
        dirs.extend(self._plugin_dirs(self.plugins_dir))
        return dirs

    def discover_bundled(self) -> list[Path]:
        """Only the image-baked bundled plugin dirs."""
        if self.bundled_plugins_dir is None:
            return []
        return self._plugin_dirs(self.bundled_plugins_dir)

    def bundled_ids(self) -> set[str]:
        """Plugin ids that ship bundled in the image (never uninstallable,
        never installable from a repo)."""
        return {d.name for d in self.discover_bundled()}

    def load_all(self, enabled_ids: Optional[set[str]] = None) -> None:
        """`enabled_ids` normally comes from InstalledPlugin.enabled rows in
        the DB; pass None to enable everything discovered (useful in tests)."""
        # User-uninstalled bundled plugins (tombstoned via the uninstall
        # endpoint) must not even load — otherwise they reappear in the
        # Installed list as disabled after every reboot.
        try:
            from models import AppSetting, db
            tombstones = {
                row.key.removeprefix("plugin.uninstalled.")
                for row in AppSetting.query.filter(AppSetting.key.like("plugin.uninstalled.%")).all()
            }
        except Exception:
            tombstones = set()
        for plugin_dir in self.discover():
            if plugin_dir.name in tombstones:
                logger.info("[PLUGINS] Skipping load of %s (uninstalled by user)", plugin_dir.name)
                continue
            try:
                loaded = self.load_plugin(plugin_dir)
            except PluginLoadError as exc:
                logger.error("Failed to load plugin at %s: %s", plugin_dir, exc)
                continue
            if enabled_ids is None or loaded.manifest.id in enabled_ids:
                self.enable_plugin(loaded.manifest.id)

    def load_plugin(self, plugin_dir: Path) -> LoadedPlugin:
        manifest = self._read_manifest(plugin_dir)
        self._check_compatibility(manifest)

        module = self._import_module(plugin_dir, manifest)
        cls_name = manifest.entry_point.split(":")[-1]
        plugin_cls = getattr(module, cls_name, None)
        if plugin_cls is None or not issubclass(plugin_cls, PluginBase):
            raise PluginLoadError(f"entry_point class '{cls_name}' not found or not a PluginBase")

        context = PluginContext(
            plugin_id=manifest.id,
            permissions=manifest.permissions,
            event_bus=self.event_bus,
            ui_slot_registry=self.ui_slot_registry,
            scheduler_hook=self._scheduler_hook,
        )
        instance = plugin_cls(context)
        instance.manifest = manifest

        loaded = LoadedPlugin(instance, manifest, plugin_dir)
        with self._lock:
            self._plugins[manifest.id] = loaded

        loaded.refresh_from_db()  # pull priority_override + any persisted state
        self.call_safe(loaded, "on_load")
        return loaded

    def _read_manifest(self, plugin_dir: Path) -> PluginManifest:
        manifest_path = plugin_dir / "plugin.json"
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginLoadError(f"invalid plugin.json: {exc}") from exc

        required = ["id", "name", "version", "type", "api_version", "entry_point"]
        missing = [f for f in required if f not in raw]
        if missing:
            raise PluginLoadError(f"plugin.json missing required fields: {missing}")

        if isinstance(raw["type"], str):
            raw["type"] = [raw["type"]]

        # HARNESS §3: warn (don't hard-fail) on types this core doesn't know —
        # the manifest type enum is forward-compatible so a plugin built for a
        # newer core still loads with a visible warning instead of breaking.
        from plugins import VALID_TYPES
        unknown = [t for t in raw["type"] if t not in VALID_TYPES]
        if unknown:
            logger.warning(
                "Plugin %s declares unknown type(s) %s (this core knows: %s)",
                raw.get("id"), unknown, sorted(VALID_TYPES),
            )

        return PluginManifest(**raw)

    @staticmethod
    def _parse_range(spec: str) -> SpecifierSet:
        """Supports plain PEP 440 specifiers ('>=1.0,<2.0') plus a caret
        shorthand ('^1.2.0' -> '>=1.2.0,<2.0.0'), which is what plugin
        manifests are expected to use."""
        if spec.startswith("^"):
            base = Version(spec[1:])
            return SpecifierSet(f">={base},<{base.major + 1}.0.0")
        return SpecifierSet(spec)

    def _check_compatibility(self, manifest: PluginManifest) -> None:
        try:
            if not self._parse_range(manifest.api_version).contains(Version(PLUGIN_API_VERSION)):
                raise PluginLoadError(
                    f"{manifest.id} requires api_version {manifest.api_version}, "
                    f"core provides {PLUGIN_API_VERSION}"
                )
            if Version(self.core_version) < Version(manifest.min_core_version):
                raise PluginLoadError(
                    f"{manifest.id} requires fnack >= {manifest.min_core_version}, "
                    f"running {self.core_version}"
                )
        except InvalidVersion as exc:
            raise PluginLoadError(f"bad version string: {exc}") from exc

    def _install_plugin_deps(self, plugin_dir: Path, manifest: PluginManifest) -> Optional[Path]:
        """Install the plugin's declared python dependencies into a private
        site-packages-style dir (Phase 4 dep isolation). Returns the deps dir
        path or None. pip failure -> PluginLoadError, never a crash."""
        deps = (manifest.dependencies or {}).get("python") or []
        if not deps:
            return None
        deps_dir = plugin_dir / "deps"
        marker = deps_dir / ".installed"
        if marker.exists():
            return deps_dir
        deps_dir.mkdir(parents=True, exist_ok=True)
        import subprocess
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
               "--target", str(deps_dir), *deps]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise PluginLoadError(
                    f"{manifest.id} dependencies failed to install: {result.stderr[-400:]}"
                )
        except PluginLoadError:
            raise
        except Exception as exc:
            raise PluginLoadError(f"{manifest.id} dependency install failed: {exc}") from exc
        marker.touch()
        return deps_dir

    def _import_module(self, plugin_dir: Path, manifest: PluginManifest):
        module_file = manifest.entry_point.split(":")[0].replace(".", "/") + ".py"
        module_path = plugin_dir / module_file
        if not module_path.exists():
            raise PluginLoadError(f"entry_point module not found: {module_path}")

        # Phase 4: per-plugin dependency isolation — the deps dir goes at the
        # FRONT of sys.path only for this plugin's import/lifetime.
        deps_dir = self._install_plugin_deps(plugin_dir, manifest)

        module_name = f"fnack_plugin_{manifest.id.replace('.', '_').replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if not spec or not spec.loader:
            raise PluginLoadError(f"could not build import spec for {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        old_front = None
        try:
            if deps_dir is not None and str(deps_dir) not in sys.path:
                # Front for this import; also appended permanently below so
                # lazy runtime imports inside plugin methods still resolve.
                sys.path.insert(0, str(deps_dir))
                old_front = str(deps_dir)
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this is untrusted code
            raise PluginLoadError(f"plugin raised on import: {exc}") from exc
        finally:
            if old_front is not None:
                try:
                    sys.path.remove(old_front)
                except ValueError:
                    pass
        if deps_dir is not None and str(deps_dir) not in sys.path:
            sys.path.append(str(deps_dir))  # keep for the plugin's runtime lifetime
        return module

    # -- lifecycle ------------------------------------------------------------

    def enable_plugin(self, plugin_id: str) -> None:
        loaded = self._plugins.get(plugin_id)
        if not loaded or loaded.enabled:
            return
        self.call_safe(loaded, "on_enable")
        loaded.enabled = True

    def disable_plugin(self, plugin_id: str) -> None:
        loaded = self._plugins.get(plugin_id)
        if not loaded or not loaded.enabled:
            return
        self.call_safe(loaded, "on_disable")
        self.event_bus.unsubscribe_all_for(plugin_id)
        loaded.enabled = False

    def unload_plugin(self, plugin_id: str) -> None:
        loaded = self._plugins.pop(plugin_id, None)
        if not loaded:
            return
        if loaded.enabled:
            self.call_safe(loaded, "on_disable")
        self.call_safe(loaded, "on_unload")
        self.ui_slot_registry.update(
            {
                slot: [c for c in contributors if c[0] != plugin_id]
                for slot, contributors in self.ui_slot_registry.items()
            }
        )

    # -- safe calling: timeout + exception guard + auto-disable ---------------

    def call_safe(self, loaded: LoadedPlugin, method_name: str, *args, timeout: float = DEFAULT_HOOK_TIMEOUT, **kwargs):
        method = getattr(loaded.instance, method_name, None)
        if method is None:
            return None
        try:
            # In a gevent-monkey-patched process (as fnack already runs),
            # gevent.Timeout wraps this cleanly; falling back to a plain call
            # keeps this module importable/testable without gevent installed.
            try:
                import gevent

                with gevent.Timeout(timeout):
                    result = method(*args, **kwargs)
            except ImportError:
                result = method(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - untrusted plugin code
            # NOTE: catch BaseException, NOT Exception — gevent.Timeout derives
            # from BaseException, and a hung plugin must be counted + auto-disabled
            # here rather than propagating out and killing the worker greenlet.
            loaded.consecutive_failures += 1
            self._buffer_health(loaded, error=str(exc)[:500])
            logger.exception("Plugin '%s'.%s failed (%d consecutive)",
                              loaded.manifest.id, method_name, loaded.consecutive_failures)
            if loaded.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error("Auto-disabling plugin '%s' after repeated failures", loaded.manifest.id)
                self.disable_plugin(loaded.manifest.id)
            return None
        else:
            loaded.consecutive_failures = 0
            self._buffer_health(loaded, error=None)
            return result

    # -- health bookkeeping: buffered, never per-hook commits ----------------
    # SQLite is single-writer even in WAL mode; the plugin health log must not
    # issue its own db.session.commit() on every hook call (that would add
    # write-lock contention scaling with plugin count). We buffer in memory and
    # flush piggybacked on core's existing commit points via flush_health().
    # See wayfinder/research/scale-to-millions.md §4.

    def _buffer_health(self, loaded: LoadedPlugin, error: Optional[str]) -> None:
        buf = getattr(self, "_health_buffer", None)
        if buf is None:
            buf = {}
            self._health_buffer = buf
        buf[loaded.manifest.id] = {
            "consecutive_failures": loaded.consecutive_failures,
            "last_error": error,
            "last_run_at": time.time(),
        }

    def flush_health(self) -> None:
        """Write buffered health rows to InstalledPlugin in ONE commit.
        Call from core's existing commit points (queue_service) or a periodic
        timer — never from inside plugin hook calls."""
        buf = getattr(self, "_health_buffer", None)
        if not buf:
            return
        try:
            from plugins.models import InstalledPlugin
            from models import db
            for plugin_id, h in buf.items():
                row = db.session.get(InstalledPlugin, plugin_id)
                if row is None:
                    continue
                row.consecutive_failures = h["consecutive_failures"]
                row.last_error = h["last_error"]
                if h.get("last_run_at"):
                    from datetime import datetime, timezone
                    row.last_run_at = datetime.fromtimestamp(h["last_run_at"], tz=timezone.utc)
            db.session.commit()
            buf.clear()
        except Exception:
            logger.exception("Plugin health flush failed")

    def _default_scheduler_hook(self, seconds: float, fn) -> None:
        try:
            import gevent
            gevent.spawn_later(seconds, fn)
        except ImportError:
            threading.Timer(seconds, fn).start()

    # -- typed accessors used by core (queue_service, metadata_service, ...) -

    def _effective_priority(self, loaded) -> int:
        """User-facing priority_override when set, else the manifest priority."""
        override = getattr(loaded, "priority_override", None)
        if override is not None:
            try:
                return int(override)
            except (TypeError, ValueError):
                pass
        return int(getattr(loaded.instance, "priority", 100) or 100)

    def _ordered(self, base_cls_name: str) -> list:
        from plugins import base as _base
        cls = getattr(_base, base_cls_name)
        plugins = [p for p in self._plugins.values()
                   if p.enabled and isinstance(p.instance, cls)]
        return [p.instance for p in sorted(plugins, key=lambda p: self._effective_priority(p))]

    def get_downloaders(self) -> list:
        return self._ordered("DownloaderPlugin")

    def get_metadata_providers(self) -> list:
        return self._ordered("MetadataProviderPlugin")

    def get_fingerprint_plugins(self) -> list:
        return self._ordered("FingerprintPlugin")

    def get_scan_triggers(self) -> list:
        return self._ordered("ScanTriggerPlugin")

    def get_library_tasks(self) -> list:
        return self._ordered("LibraryTaskPlugin")

    def get_ui_slot_html(self, slot_name: str, context_data: dict) -> str:
        """Called by the `plugin_slot()` Jinja helper (see INTEGRATION.md)."""
        fragments = []
        for plugin_id, render_fn in self.ui_slot_registry.get(slot_name, []):
            loaded = self._plugins.get(plugin_id)
            if loaded and loaded.enabled:
                try:
                    fragments.append(render_fn(context_data))
                except Exception:
                    logger.exception("UI slot render failed for plugin '%s' slot '%s'", plugin_id, slot_name)
        return "\n".join(f for f in fragments if f)

    def list_loaded(self) -> list[dict]:
        bundled_ids = self.bundled_ids()
        return [
            {
                "id": p.manifest.id,
                "name": p.manifest.name,
                "version": p.manifest.version,
                "type": p.manifest.type,
                "enabled": p.enabled,
                "trust_level": p.manifest.trust_level,
                "consecutive_failures": p.consecutive_failures,
                "priority": self._effective_priority(p),
                "priority_override": p.priority_override,
                "bundled": p.manifest.id in bundled_ids,
                # Per-plugin settings (user requirement): expose the declared
                # schema so the UI can render each plugin's own settings form.
                "settings_schema": p.manifest.settings_schema or [],
            }
            for p in self._plugins.values()
        ]


# A module-level singleton, created at app startup (see INTEGRATION.md) and
# imported by app.py / queue_service.py wherever plugin-provided behavior is needed.
plugin_manager: Optional[PluginManager] = None


def init_plugin_manager(plugins_dir: str, core_version: str,
                        bundled_plugins_dir: str | Path | None = None) -> PluginManager:
    global plugin_manager
    plugin_manager = PluginManager(plugins_dir=plugins_dir,
                                   bundled_plugins_dir=bundled_plugins_dir,
                                   core_version=core_version)
    return plugin_manager
