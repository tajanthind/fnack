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


class PluginLoadError(Exception):
    pass


class LoadedPlugin:
    def __init__(self, instance: PluginBase, manifest: PluginManifest, module_path: Path):
        self.instance = instance
        self.manifest = manifest
        self.module_path = module_path
        self.enabled = False
        self.consecutive_failures = 0


class PluginManager:
    def __init__(self, plugins_dir: str | Path = "/config/plugins", core_version: str = "0.0.0"):
        self.plugins_dir = Path(plugins_dir)
        self.core_version = core_version
        self.event_bus = EventBus()
        self.ui_slot_registry: dict[str, list] = {}
        self._plugins: dict[str, LoadedPlugin] = {}
        self._lock = threading.Lock()
        self._scheduler_hook = self._default_scheduler_hook

    # -- discovery & loading -------------------------------------------------

    def discover(self) -> list[Path]:
        if not self.plugins_dir.exists():
            return []
        return [p for p in self.plugins_dir.iterdir() if p.is_dir() and (p / "plugin.json").exists()]

    def load_all(self, enabled_ids: Optional[set[str]] = None) -> None:
        """`enabled_ids` normally comes from InstalledPlugin.enabled rows in
        the DB; pass None to enable everything discovered (useful in tests)."""
        for plugin_dir in self.discover():
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

    def _import_module(self, plugin_dir: Path, manifest: PluginManifest):
        module_file = manifest.entry_point.split(":")[0].replace(".", "/") + ".py"
        module_path = plugin_dir / module_file
        if not module_path.exists():
            raise PluginLoadError(f"entry_point module not found: {module_path}")

        module_name = f"fnack_plugin_{manifest.id.replace('.', '_').replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if not spec or not spec.loader:
            raise PluginLoadError(f"could not build import spec for {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this is untrusted code
            raise PluginLoadError(f"plugin raised on import: {exc}") from exc
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
        except Exception as exc:  # noqa: BLE001 - untrusted plugin code
            loaded.consecutive_failures += 1
            logger.exception("Plugin '%s'.%s failed (%d consecutive)",
                              loaded.manifest.id, method_name, loaded.consecutive_failures)
            if loaded.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error("Auto-disabling plugin '%s' after repeated failures", loaded.manifest.id)
                self.disable_plugin(loaded.manifest.id)
            return None
        else:
            loaded.consecutive_failures = 0
            return result

    def _default_scheduler_hook(self, seconds: float, fn) -> None:
        try:
            import gevent
            gevent.spawn_later(seconds, fn)
        except ImportError:
            threading.Timer(seconds, fn).start()

    # -- typed accessors used by core (queue_service, metadata_service, ...) -

    def get_downloaders(self) -> list:
        from plugins.base import DownloaderPlugin
        plugins = [p.instance for p in self._plugins.values()
                   if p.enabled and isinstance(p.instance, DownloaderPlugin)]
        return sorted(plugins, key=lambda p: p.priority)

    def get_metadata_providers(self) -> list:
        from plugins.base import MetadataProviderPlugin
        plugins = [p.instance for p in self._plugins.values()
                   if p.enabled and isinstance(p.instance, MetadataProviderPlugin)]
        return sorted(plugins, key=lambda p: p.priority)

    def get_fingerprint_plugins(self) -> list:
        from plugins.base import FingerprintPlugin
        return [p.instance for p in self._plugins.values()
                if p.enabled and isinstance(p.instance, FingerprintPlugin)]

    def get_scan_triggers(self) -> list:
        from plugins.base import ScanTriggerPlugin
        return [p.instance for p in self._plugins.values()
                if p.enabled and isinstance(p.instance, ScanTriggerPlugin)]

    def get_library_tasks(self) -> list:
        from plugins.base import LibraryTaskPlugin
        return [p.instance for p in self._plugins.values()
                if p.enabled and isinstance(p.instance, LibraryTaskPlugin)]

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
        return [
            {
                "id": p.manifest.id,
                "name": p.manifest.name,
                "version": p.manifest.version,
                "type": p.manifest.type,
                "enabled": p.enabled,
                "trust_level": p.manifest.trust_level,
                "consecutive_failures": p.consecutive_failures,
            }
            for p in self._plugins.values()
        ]


# A module-level singleton, created at app startup (see INTEGRATION.md) and
# imported by app.py / queue_service.py wherever plugin-provided behavior is needed.
plugin_manager: Optional[PluginManager] = None


def init_plugin_manager(plugins_dir: str, core_version: str) -> PluginManager:
    global plugin_manager
    plugin_manager = PluginManager(plugins_dir=plugins_dir, core_version=core_version)
    return plugin_manager
