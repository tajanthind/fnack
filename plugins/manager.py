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

import ast
import importlib.util
import json
import logging
import sys
try:
    from gevent.lock import RLock as _ManagerRLock  # cooperative (fnack runs gevent)
except ImportError:  # pragma: no cover - non-gevent context (tests/scripts)
    from threading import RLock as _ManagerRLock
import time
from pathlib import Path
from typing import Optional

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from plugins import PLUGIN_API_VERSION
from plugins.base import PluginBase, PluginManifest
from plugins.context import PluginContext
from plugins.events import EventBus

# Modules that give plugin code real network access WITHOUT going through
# context.http. Importing one of these while the manifest does NOT declare
# "network" is a manifest-contract bypass — detected at load and warned
# about (see _network_capable_imports / load_plugin). Declaring "network" is
# the honest signal; the scanner never blocks, it reports. 'urllib'/'http'
# are package roots (their .request/.client submodules do the network I/O);
# flagging a bare `import urllib` is a rare, harmless over-reach for a
# warning-only scan.
_NETWORK_IMPORT_MODULES = {
    "requests", "urllib", "urllib.request", "http", "http.client",
    "urllib3", "httpx", "aiohttp", "websocket", "websockets", "socket",
}


def _network_capable_imports(plugin_dir: Path) -> list[str]:
    """AST-scan a plugin directory for imports of network-capable modules.

    Returns the sorted ROOT module names found (e.g. ['requests', 'socket']),
    so `from requests.adapters import HTTPAdapter` reports 'requests'.
    Pure static scan — nothing is executed; unparseable files are skipped.
    """
    found: set[str] = set()
    py_files = sorted(plugin_dir.rglob("*.py")) if plugin_dir.is_dir() else []
    for py in py_files:
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for m in _NETWORK_IMPORT_MODULES:
                        if alias.name == m or alias.name.startswith(m + "."):
                            found.add(m)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for m in _NETWORK_IMPORT_MODULES:
                    if mod == m or mod.startswith(m + "."):
                        found.add(m)
    return sorted(found)

# Phase 1 (MASTER): capability registry is separate from PluginManager.
from fnack.plugin_api.capabilities import (
    ALBUM_METADATA,
    ARTIST_DISCOGRAPHY,
    ARTIST_SEARCH,
    AUTH_PROVIDER,
    DOWNLOAD_TRACK,
    FINGERPRINT_IDENTIFY,
    LIBRARY_TASK,
    MEDIA_CONNECTION_TEST,
    MEDIA_SCAN,
    NETWORK_ROUTE,
    NOTIFICATION_EVENT,
    SERVER_EXTENSION,
    TRACK_METADATA,
    TRACK_RESOLVE,
    CapabilityRegistry,
)
from fnack.plugin_api.errors import CapabilityUnavailable
from fnack.plugin_api.providers import ProviderExecutor
from fnack.plugin_api.contracts import validate_capability_contract

logger = logging.getLogger("fnack.plugins.manager")

# Fallback capability derivation when a manifest omits `capabilities`:
# plugin `type` -> capability IDs it implies (MASTER §5, PHASE 1
# §Manifest capability declaration). Manifests may declare a different or
# richer set; unknown types derive nothing (warned at load, forward-compatible).
TYPE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "downloader": (DOWNLOAD_TRACK,),
    "metadata_provider": (ARTIST_SEARCH, ARTIST_DISCOGRAPHY, TRACK_METADATA, ALBUM_METADATA),
    "fingerprint": (FINGERPRINT_IDENTIFY,),
    "scan_trigger": (MEDIA_SCAN, MEDIA_CONNECTION_TEST),
    "library_task": (LIBRARY_TASK,),
    "vpn": (NETWORK_ROUTE,),
    "server_extension": (SERVER_EXTENSION,),
    "auth_provider": (AUTH_PROVIDER,),
    "event_hook": (NOTIFICATION_EVENT,),
    # track.resolve is not implied by any current type — plugins that do
    # URL resolution (e.g. fnack.spotify) declare it explicitly.
    "lyrics_provider": (),
    "storage_backend": (),
    "ui_extension": (),
    "library_source": (),
    "conflict_resolver": (),
    "recommendation": (),
}

MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_HOOK_TIMEOUT = 10.0
# Downloads legitimately take minutes (network fetch + verify); the 10s
# default would kill a real download mid-flight. The queue passes this for
# `download` calls, matching the old 180s service-level timeouts.
DOWNLOAD_HOOK_TIMEOUT = 600.0


class PluginLoadError(Exception):
    pass


class VersionMismatchError(PluginLoadError):
    """A plugin whose api_version/min_core_version is incompatible with the
    running core. Distinct from a crash so the UI can show a clear
    'Unsupported — requires core ≥ X, you're on Y' state (Brief 6 §4)."""


class LoadedPlugin:
    def __init__(self, instance: PluginBase, manifest: PluginManifest, module_path: Path):
        self.instance = instance
        self.manifest = manifest
        self.module_path = module_path
        self.enabled = False
        self.consecutive_failures = 0
        self.priority_override: Optional[int] = None  # user override, from InstalledPlugin.priority_override
        # Phase 1: capability IDs this plugin provides (manifest-declared or
        # type-derived). Filled in by PluginManager.load_plugin.
        self.capabilities: list[str] = []
        # Phase 1.1: capability-specific priority overrides
        # {capability_id: priority} loaded from PluginCapabilityPriority.
        # Absent capability = use plugin-level default (priority_override or
        # manifest priority). LOWER number = tried first.
        self.capability_priorities: dict[str, int] = {}

    def refresh_from_db(self) -> None:
        """Pull persisted per-install state (priority_override + per-
        capability priority overrides) from the DB."""
        try:
            from plugins.models import InstalledPlugin, PluginCapabilityPriority
            from models import db
            row = db.session.get(InstalledPlugin, self.manifest.id)
            if row is not None:
                self.priority_override = row.priority_override
            cap_rows = PluginCapabilityPriority.query.filter_by(plugin_id=self.manifest.id).all()
            self.capability_priorities = {
                r.capability_id: int(r.priority) for r in cap_rows
            }
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
        # Reverse index id(instance) -> plugin_id so _loaded_for_instance is
        # O(1) instead of a linear scan on every invoke_provider().
        self._instance_to_id: dict[int, str] = {}
        self._lock = _ManagerRLock()
        self._scheduler_hook = self._default_scheduler_hook
        # Phase 1: the capability registry — separate concern from plugin
        # lifecycle. Application services query this; they never name a
        # provider implementation.
        self.capability_registry = CapabilityRegistry()
        # Central sync/async executor (Phase 1 §Async provider executor).
        self.executor = ProviderExecutor()
        # Brief 6 §4: plugins that failed to load, keyed by plugin id, with
        # the reason (e.g. version mismatch). They must still SHOW in the
        # Installed list as "Unsupported / failed to load" instead of
        # silently vanishing.
        self._load_failures: dict[str, str] = {}
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
                self._load_failures[plugin_dir.name] = str(exc)
                logger.error("Failed to load plugin at %s: %s", plugin_dir, exc)
                continue
            self._load_failures.pop(plugin_dir.name, None)
            if enabled_ids is None or loaded.manifest.id in enabled_ids:
                self.enable_plugin(loaded.manifest.id)

    def load_plugin(self, plugin_dir: Path) -> LoadedPlugin:
        manifest = self._read_manifest(plugin_dir)
        self._check_compatibility(manifest)

        # Honest-network check: importing requests/urllib/socket/etc. while
        # the manifest does NOT declare "network" bypasses the context.http
        # gate. Never a block (static scans can false-positive; in-process
        # plugins can only be trusted, not sandboxed) — but it is surfaced
        # loudly so authors notice the contract drift. Bundled official
        # plugins are held to the same rule by an architecture test.
        if "network" not in (manifest.permissions or []):
            try:
                imports = _network_capable_imports(plugin_dir)
            except Exception:
                imports = []
            if imports:
                logger.warning(
                    "[PLUGINS] %s imports network-capable module(s) %s but does not "
                    "declare 'network' in its manifest — this bypasses context.http "
                    "(the permission gate). Declare the permission or route traffic "
                    "through context.http.",
                    manifest.id, ", ".join(imports),
                )

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
            settings_schema=manifest.settings_schema,
        )
        instance = plugin_cls(context)
        instance.manifest = manifest

        loaded = LoadedPlugin(instance, manifest, plugin_dir)
        with self._lock:
            self._plugins[manifest.id] = loaded
            self._instance_to_id[id(instance)] = manifest.id

        loaded.refresh_from_db()  # pull priority_override + any persisted state
        # Phase 1: compute this plugin's capabilities (manifest-declared or
        # derived from its type(s)); actual registry registration happens on
        # enable so the registry only ever reflects ENABLED providers.
        loaded.capabilities = self._resolve_capabilities(loaded)
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

        # Phase 1 (MASTER): validate declared capabilities the same way —
        # warn on unknown IDs (forward-compatible), don't refuse to load.
        from fnack.plugin_api.capabilities import ALL_CAPABILITIES
        declared_caps = raw.get("capabilities") or []
        if isinstance(declared_caps, str):
            declared_caps = [declared_caps]
        unknown_caps = [c for c in declared_caps if c not in ALL_CAPABILITIES]
        if unknown_caps:
            logger.warning(
                "Plugin %s declares unknown capability id(s) %s (known: %s)",
                raw.get("id"), unknown_caps, sorted(ALL_CAPABILITIES),
            )
        raw["capabilities"] = [str(c) for c in declared_caps]

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
                raise VersionMismatchError(
                    f"{manifest.id} requires api_version {manifest.api_version}, "
                    f"core provides {PLUGIN_API_VERSION}"
                )
            if Version(self.core_version) < Version(manifest.min_core_version):
                raise VersionMismatchError(
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
            # Phase 2: multi-file plugins (e.g. fnack.spotiflac ships
            # spotiflac.py alongside plugin.py). The plugin dir must be on
            # sys.path so `import spotiflac` inside plugin.py resolves; put it
            # at the FRONT for this import and keep it appended (like deps) for
            # the plugin's runtime lifetime.
            if str(plugin_dir) not in sys.path:
                sys.path.insert(0, str(plugin_dir))
                old_front = str(plugin_dir)
            if deps_dir is not None and str(deps_dir) not in sys.path:
                # Front for this import; also appended permanently below so
                # lazy runtime imports inside plugin methods still resolve.
                sys.path.insert(0, str(deps_dir))
                old_front = old_front or str(deps_dir)
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this is untrusted code
            raise PluginLoadError(f"plugin raised on import: {exc}") from exc
        finally:
            if old_front is not None:
                try:
                    sys.path.remove(old_front)
                except ValueError:
                    pass
        if str(plugin_dir) not in sys.path:
            sys.path.append(str(plugin_dir))  # keep for the plugin's runtime lifetime
        if deps_dir is not None and str(deps_dir) not in sys.path:
            sys.path.append(str(deps_dir))  # keep for the plugin's runtime lifetime
        return module

    # -- lifecycle ------------------------------------------------------------

    def _resolve_capabilities(self, loaded: LoadedPlugin) -> list[str]:
        """Capability IDs this plugin provides: manifest-declared wins;
        otherwise derived from the plugin's `type`(s).

        Phase 1.1 §2: every candidate is validated against its capability
        contract (the required interface methods). Invalid capabilities are
        SKIPPED (not the whole plugin) with a clear warning — one plugin can
        provide several independent capabilities, and a bad declaration must
        not take down the valid ones or produce a cryptic AttributeError at
        invocation time."""
        declared = [str(c) for c in (loaded.manifest.capabilities or [])]
        if declared:
            candidates = declared
        else:
            derived: list[str] = []
            for t in loaded.manifest.type or []:
                derived.extend(TYPE_CAPABILITIES.get(t, ()))
            candidates = list(dict.fromkeys(derived))

        valid: list[str] = []
        for cap in candidates:
            missing = validate_capability_contract(loaded.manifest.id, cap, loaded.instance)
            if missing:
                logger.warning(
                    "Plugin '%s' declares capability '%s' but does not implement it "
                    "(missing: %s) — capability NOT registered. See "
                    "fnack.plugin_api.contracts for the expected interface.",
                    loaded.manifest.id, cap, ", ".join(missing),
                )
                continue
            valid.append(cap)
        return valid

    def _register_capabilities(self, loaded: LoadedPlugin) -> None:
        self.capability_registry.register(
            plugin_id=loaded.manifest.id,
            provider=loaded.instance,
            capabilities=loaded.capabilities,
            priority=self._effective_priority(loaded),
            priorities={
                cap: self._effective_priority(loaded, capability=cap)
                for cap in loaded.capabilities
            },
        )

    def _unregister_capabilities(self, plugin_id: str) -> None:
        self.capability_registry.unregister_plugin(plugin_id)

    def enable_plugin(self, plugin_id: str) -> None:
        loaded = self._plugins.get(plugin_id)
        if not loaded or loaded.enabled:
            return
        self.call_safe(loaded, "on_enable")
        loaded.enabled = True
        # Phase 1: enabling a plugin makes its capabilities available.
        if loaded.capabilities:
            self._register_capabilities(loaded)

    def disable_plugin(self, plugin_id: str) -> None:
        loaded = self._plugins.get(plugin_id)
        if not loaded or not loaded.enabled:
            return
        self.call_safe(loaded, "on_disable")
        self.event_bus.unsubscribe_all_for(plugin_id)
        loaded.enabled = False
        # Phase 1: disabling a plugin removes its capabilities (MASTER rule 2:
        # "If an official plugin is disabled or removed, its capability
        # disappears. Core must not silently fall back to a hidden
        # implementation.").
        self._unregister_capabilities(plugin_id)

    def unload_plugin(self, plugin_id: str) -> None:
        with self._lock:
            loaded = self._plugins.pop(plugin_id, None)
            if loaded is not None:
                # Keep the reverse index in sync (avoid stale entries keyed by
                # a recycled id()).
                self._instance_to_id.pop(id(loaded.instance), None)
        if not loaded:
            return
        if loaded.enabled:
            self.call_safe(loaded, "on_disable")
        self.call_safe(loaded, "on_unload")
        self._unregister_capabilities(plugin_id)
        self.ui_slot_registry.update(
            {
                slot: [c for c in contributors if c[0] != plugin_id]
                for slot, contributors in self.ui_slot_registry.items()
            }
        )

    # -- safe calling: timeout + exception guard + auto-disable ---------------

    def _loaded_for_instance(self, instance) -> Optional[LoadedPlugin]:
        """Reverse-lookup: the LoadedPlugin whose `.instance` is `instance`.
        Used by invoke_provider when call sites hold a provider INSTANCE
        (e.g. from get_metadata_providers()/get_scan_triggers()) instead of
        the LoadedPlugin wrapper."""
        plugin_id = self._instance_to_id.get(id(instance))
        if plugin_id is None:
            return None
        return self._plugins.get(plugin_id)

    def invoke_provider(
        self,
        loaded: LoadedPlugin | object,
        method_name: str,
        *args,
        timeout: float = DOWNLOAD_HOOK_TIMEOUT,
        **kwargs,
    ):
        """RUNTIME provider invocation boundary (Phase 1.1 §3).

        Application services invoke capability providers through THIS method
        — never a raw method call, never `self.executor.run` directly, and
        never `asyncio.run()` scattered through providers. The executor
        handles sync methods, async methods, awaitable results, timeouts,
        and provider-error normalization in one place; the manager wraps it
        in the gevent timeout + consecutive-failure + auto-disable guard so
        a hung or crashing provider is retired safely (queue behavior
        unchanged).

        `loaded` may be a LoadedPlugin (from get_loaded()) or a provider
        INSTANCE (from get_metadata_providers()/get_scan_triggers()/the
        capability registry) — the manager resolves the instance back to its
        LoadedPlugin for the guard.

        Lifecycle hooks (on_load/on_enable/...) may keep using call_safe;
        runtime capability invocation must use this path.
        """
        if not isinstance(loaded, LoadedPlugin):
            loaded = self._loaded_for_instance(loaded)
            if loaded is None:
                return None
        method = getattr(loaded.instance, method_name, None)
        if method is None:
            return None
        try:
            # gevent.Timeout guards the WHOLE invocation (sync + async);
            # ProviderExecutor.run drives any awaitable centrally.
            try:
                import gevent

                with gevent.Timeout(timeout):
                    result = self.executor.run(
                        loaded.instance, method_name, *args, **kwargs
                    )
            except ImportError:
                result = self.executor.run(
                    loaded.instance, method_name, *args, **kwargs
                )
        except BaseException as exc:  # noqa: BLE001 - untrusted plugin code
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

    def _effective_priority(self, loaded, capability: Optional[str] = None) -> int:
        """Effective priority for a plugin, optionally per capability
        (Phase 1.1). Resolution chain, LOWEST number = tried first:
            capability-specific override > plugin-level priority_override
            > manifest/class priority.

        Passing no capability returns the plugin-level effective priority
        (used by the legacy type-ordered getters)."""
        if capability is not None:
            cap_override = (loaded.capability_priorities or {}).get(capability)
            if cap_override is not None:
                try:
                    return int(cap_override)
                except (TypeError, ValueError):
                    pass
        override = getattr(loaded, "priority_override", None)
        if override is not None:
            try:
                return int(override)
            except (TypeError, ValueError):
                pass
        return int(getattr(loaded.instance, "priority", 100) or 100)

    # -- Phase 1.1: capability-specific priority configuration ---------------

    def get_capability_priorities(self, plugin_id: str) -> dict[str, int]:
        """{capability_id: effective priority} for a plugin's capabilities.
        Each value is the capability-specific override if set, else the
        plugin-level effective priority. Empty dict if plugin unknown."""
        loaded = self._plugins.get(plugin_id)
        if loaded is None:
            return {}
        return {
            cap: self._effective_priority(loaded, capability=cap)
            for cap in (loaded.capabilities or [])
        }

    def set_capability_priority(
        self,
        plugin_id: str,
        capability_id: str,
        priority: Optional[int],
    ) -> dict[str, int]:
        """Set (or clear with None) the capability-specific priority override
        for one (plugin, capability). Validates the capability is declared.
        Returns the updated {capability: effective priority} map.

        Raises ValueError if the plugin or capability is unknown, or the
        priority is < 1."""
        loaded = self._plugins.get(plugin_id)
        if loaded is None:
            raise ValueError(f"no such plugin: {plugin_id}")
        if capability_id not in (loaded.capabilities or []):
            raise ValueError(
                f"plugin '{plugin_id}' does not declare capability '{capability_id}'"
            )
        if priority is not None and int(priority) < 1:
            raise ValueError("priority must be >= 1")

        # Persist via the existing DB (same session as priority_override).
        # Outside an app context (architecture tests, bare manager) the
        # override still applies in-memory — matching refresh_from_db's
        # tolerance.
        try:
            from plugins.models import PluginCapabilityPriority
            from models import db
            row = db.session.get(PluginCapabilityPriority, (plugin_id, capability_id))
            if priority is None:
                if row is not None:
                    db.session.delete(row)
                loaded.capability_priorities.pop(capability_id, None)
            else:
                value = int(priority)
                if row is None:
                    db.session.add(PluginCapabilityPriority(
                        plugin_id=plugin_id, capability_id=capability_id, priority=value,
                    ))
                else:
                    row.priority = value
                loaded.capability_priorities[capability_id] = value
            db.session.commit()
        except Exception:
            logger.debug(
                "Could not persist capability priority for %s/%s (in-memory only)",
                plugin_id, capability_id, exc_info=True,
            )
            if priority is None:
                loaded.capability_priorities.pop(capability_id, None)
            else:
                loaded.capability_priorities[capability_id] = int(priority)
        # Keep the registry's ordering in sync.
        self.refresh_capability_registration(plugin_id)
        return self.get_capability_priorities(plugin_id)

    def _ordered(self, base_cls_name: str) -> list:
        from plugins import base as _base
        cls = getattr(_base, base_cls_name)
        plugins = [p for p in self._plugins.values()
                   if p.enabled and isinstance(p.instance, cls)]
        return [p.instance for p in sorted(plugins, key=lambda p: self._effective_priority(p))]

    def get_downloaders(self) -> list:
        """Enabled download.track providers (Phase 2: SDK-contract plugins
        like fnack.spotiflac implement TrackDownloader, legacy ones still
        subclass DownloaderPlugin — both serve the same capability)."""
        from fnack.plugin_api.capabilities import DOWNLOAD_TRACK
        handles = self.capability_registry.providers_for(DOWNLOAD_TRACK)
        if handles:
            return [h.provider for h in handles]
        return self._ordered("DownloaderPlugin")

    def get_metadata_providers(self) -> list:
        return self._ordered("MetadataProviderPlugin")

    def get_fingerprint_plugins(self) -> list:
        return self._ordered("FingerprintPlugin")

    def get_scan_triggers(self) -> list:
        return self._ordered("ScanTriggerPlugin")

    # -- Phase 1 public API: no private `_plugins` access from app services ---

    def get_plugin(self, plugin_id: str) -> Optional[PluginBase]:
        """The plugin instance, or None. (Public replacement for reaching
        into `manager._plugins[...]`.)"""
        loaded = self._plugins.get(plugin_id)
        return loaded.instance if loaded else None

    def get_loaded(self, plugin_id: str) -> Optional[LoadedPlugin]:
        """The LoadedPlugin wrapper (instance, manifest, enabled, failures,
        priority_override), or None."""
        return self._plugins.get(plugin_id)

    def get_plugin_context(self, plugin_id: str) -> Optional[PluginContext]:
        """The plugin's PluginContext, or None."""
        loaded = self._plugins.get(plugin_id)
        return getattr(loaded.instance, "context", None) if loaded else None

    def get_plugin_capabilities(self, plugin_id: str) -> list[str]:
        """Capability IDs this plugin provides (declared or derived), or [].
        Independent of enabled state — use the capability registry to know
        which providers are actually available."""
        loaded = self._plugins.get(plugin_id)
        return list(loaded.capabilities) if loaded else []

    def get_capability_providers(self, capability: str) -> list:
        """Enabled provider instances for a capability, priority-ordered
        (lowest priority number first). Raises CapabilityUnavailable when no
        enabled plugin provides it."""
        handles = self.capability_registry.providers(capability)
        if not handles:
            raise CapabilityUnavailable(capability=capability, operation="get_capability_providers")
        return [h.provider for h in handles]

    def has_capability(self, capability: str) -> bool:
        return self.capability_registry.has(capability)

    def refresh_capability_registration(self, plugin_id: str) -> None:
        """Re-register a plugin's capabilities after its effective priority
        changed (priority_override) — the registry keeps the new ordering."""
        loaded = self._plugins.get(plugin_id)
        if loaded is None:
            return
        self._unregister_capabilities(plugin_id)
        if loaded.enabled and loaded.capabilities:
            self._register_capabilities(loaded)

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
        out = [
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
                # Brief 5 §4: surface the manifest description on /plugins.
                "description": p.manifest.description or "",
                # Brief 6 §2: imperative actions rendered in the settings modal.
                "actions": p.manifest.actions or [],
                # Phase 1: capability IDs this plugin provides.
                "capabilities": list(p.capabilities),
                # Phase 1.1: {capability_id: effective priority} — per-
                # capability ordering (LOWER = tried first).
                "capability_priorities": {
                    cap: self._effective_priority(p, capability=cap)
                    for cap in (p.capabilities or [])
                },
                "load_error": None,
            }
            for p in self._plugins.values()
        ]
        # Brief 6 §4: plugins that failed to load still appear, badged with
        # the reason (version mismatch -> "Unsupported"; other -> error).
        for pid, reason in self._load_failures.items():
            out.append({
                "id": pid,
                "name": pid,
                "version": "?",
                "type": [],
                "enabled": False,
                "trust_level": "community",
                "consecutive_failures": 0,
                "priority": 100,
                "priority_override": None,
                "bundled": pid in bundled_ids,
                "settings_schema": [],
                "description": "",
                "actions": [],
                "capabilities": [],
                "capability_priorities": {},
                "load_error": reason,
            })
        return out


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
