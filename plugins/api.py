"""REST endpoints for plugin management. Registered in app.py as:

    from plugins.api import build_plugins_blueprint
    app.register_blueprint(build_plugins_blueprint(plugin_manager, plugin_registry))

Kept as a factory (rather than importing a bare module-level manager) so
tests can build a blueprint against a throwaway PluginManager/PluginRegistry.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from models import db
from plugins.manager import PluginManager
from plugins.models import InstalledPlugin, PluginRepository, PluginSetting
from plugins.registry import PluginRegistry, RegistryError


def _ensure_installed_row(manager: PluginManager, plugin_id: str, enabled: bool = True) -> InstalledPlugin:
    """Create an InstalledPlugin row when missing (e.g. a manual folder install
    or a bundled plugin enabled before auto-install ran). Enabling a plugin
    must persist across restarts — a row-less enable currently reverts."""
    row = db.session.get(InstalledPlugin, plugin_id)
    if row is not None:
        row.enabled = enabled
        db.session.commit()
        return row
    loaded = manager.get_loaded(plugin_id)
    manifest = loaded.manifest if loaded else None
    row = InstalledPlugin(
        id=plugin_id,
        name=(manifest.name if manifest else plugin_id),
        version=(manifest.version if manifest else "0.0.0"),
        type=",".join(manifest.type) if manifest else "",
        enabled=enabled,
        trust_level=(manifest.trust_level if manifest else "community"),
        source_repo_id=None,
        manifest_json="{}",
    )
    db.session.add(row)
    db.session.commit()
    return row


def build_plugins_blueprint(manager: PluginManager, registry: PluginRegistry) -> Blueprint:
    bp = Blueprint("plugins", __name__, url_prefix="/api/plugins")

    # -- installed plugins -------------------------------------------------

    @bp.route("", methods=["GET"])
    def list_installed():
        loaded = manager.list_loaded()
        # Brief 6 §3: flag plugins whose source repo offers a newer version.
        # Bundled plugins ship with the image — only marketplace-installed
        # plugins can update independently (decided: no Update for bundled).
        latest = {}
        try:
            latest = registry.latest_versions()
        except Exception:
            pass
        bundled_ids = manager.bundled_ids()
        for p in loaded:
            lv = latest.get(p["id"])
            p["latest_version"] = lv
            p["update_available"] = bool(
                lv and not p["bundled"] and p["id"] not in bundled_ids and lv != p["version"]
            )
        return jsonify(loaded)

    @bp.route("/<plugin_id>/update", methods=["POST"])
    def update_plugin(plugin_id):
        """Update an installed plugin to its source repo's latest version
        (Brief 6 §3). Settings (PluginSetting rows) are keyed by plugin_id and
        survive. Bundled plugins are refused — they update with the fnack
        image, not independently."""
        if plugin_id in manager.bundled_ids():
            return jsonify({"error": "Bundled plugins update with the fnack release (docker compose pull && up -d), not independently."}), 400
        try:
            row = registry.update(plugin_id)
        except RegistryError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "id": row.id, "version": row.version})

    @bp.route("/grouped", methods=["GET"])
    def list_grouped():
        """Plugins grouped by type for the Settings → Plugins page.
        A plugin implementing multiple types appears once per type."""
        loaded = manager.list_loaded()
        grouped: dict[str, list] = {}
        for p in loaded:
            for t in p["type"]:
                grouped.setdefault(t, []).append(p)
        return jsonify(grouped)

    @bp.route("/<plugin_id>/enable", methods=["POST"])
    def enable(plugin_id):
        manager.enable_plugin(plugin_id)
        _ensure_installed_row(manager, plugin_id, enabled=True)
        return jsonify({"ok": True})

    @bp.route("/<plugin_id>/disable", methods=["POST"])
    def disable(plugin_id):
        manager.disable_plugin(plugin_id)
        _ensure_installed_row(manager, plugin_id, enabled=False)
        return jsonify({"ok": True})

    @bp.route("/<plugin_id>/priority", methods=["POST"])
    def set_priority(plugin_id):
        """Set the user-facing priority override for ordered plugin types.
        `priority` is a positive int; NULL clears the override (falls back to
        the manifest priority). This is the PLUGIN-LEVEL default — it applies
        to every capability the plugin provides unless a capability-specific
        override is set (see /capabilities/<cap>/priority)."""
        payload = request.get_json(silent=True) or {}
        if "priority" not in payload:
            return jsonify({"error": "priority is required"}), 400
        raw = payload.get("priority")
        try:
            value = None if raw is None else int(raw)
        except (TypeError, ValueError):
            return jsonify({"error": "priority must be an integer"}), 400
        if value is not None and value < 1:
            return jsonify({"error": "priority must be >= 1"}), 400
        row = _ensure_installed_row(manager, plugin_id, enabled=True)
        row.priority_override = value
        db.session.commit()
        loaded = manager.get_loaded(plugin_id)
        if loaded:
            loaded.priority_override = value
            # Phase 1: keep the capability registry's ordering in sync.
            manager.refresh_capability_registration(plugin_id)
        return jsonify({"ok": True, "priority": value})

    # -- Phase 1.1: capability-specific priority -----------------------------

    @bp.route("/<plugin_id>/capabilities", methods=["GET"])
    def get_capabilities(plugin_id):
        """List a plugin's capabilities with their EFFECTIVE priority
        (LOWER = tried first) and the source of that priority:
        "capability" (per-capability override), "plugin" (plugin-level
        priority_override), or "manifest" (class priority)."""
        loaded = manager.get_loaded(plugin_id)
        if loaded is None:
            return jsonify({"error": "plugin not loaded"}), 404
        plugin_override = loaded.priority_override
        manifest_priority = int(getattr(loaded.instance, "priority", 100) or 100)
        out = []
        for cap in loaded.capabilities or []:
            cap_override = (loaded.capability_priorities or {}).get(cap)
            if cap_override is not None:
                source, priority = "capability", int(cap_override)
            elif plugin_override is not None:
                source, priority = "plugin", int(plugin_override)
            else:
                source, priority = "manifest", manifest_priority
            out.append({
                "capability_id": cap,
                "priority": priority,
                "source": source,
            })
        return jsonify({"plugin_id": plugin_id, "capabilities": out})

    @bp.route("/<plugin_id>/capabilities/<capability>/priority", methods=["POST"])
    def set_capability_priority(plugin_id, capability):
        """Set/clear the capability-specific priority override for one
        (plugin, capability). Body: {"priority": N} (N >= 1) or
        {"priority": null} to clear (falls back to the plugin-level default).
        LOWER number = tried first."""
        payload = request.get_json(silent=True) or {}
        if "priority" not in payload:
            return jsonify({"error": "priority is required"}), 400
        raw = payload.get("priority")
        try:
            value = None if raw is None else int(raw)
        except (TypeError, ValueError):
            return jsonify({"error": "priority must be an integer"}), 400
        if value is not None and value < 1:
            return jsonify({"error": "priority must be >= 1"}), 400
        try:
            updated = manager.set_capability_priority(plugin_id, capability, value)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "plugin_id": plugin_id,
                        "capability_id": capability, "priority": value,
                        "capability_priorities": updated})

    @bp.route("/<plugin_id>/uninstall", methods=["POST"])
    def uninstall(plugin_id):
        # Bundled plugins ship in the image and would auto-install again on
        # boot — record a tombstone so auto-install skips them, then remove.
        was_bundled = plugin_id in manager.bundled_ids()
        if was_bundled:
            from models import AppSetting
            row = db.session.get(AppSetting, f"plugin.uninstalled.{plugin_id}")
            if row is None:
                db.session.add(AppSetting(key=f"plugin.uninstalled.{plugin_id}", value="1"))
            else:
                row.value = "1"
            db.session.commit()
        try:
            registry.uninstall(plugin_id)
        except RegistryError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @bp.route("/<plugin_id>/file", methods=["POST"])
    def upload_file(plugin_id):
        """Generic per-plugin file upload (Brief 4 §3 — the reusable upload
        tool). Each plugin stores its OWN copy under its private data dir;
        this is shared *tooling*, not shared *credentials* (option 2, no
        cross-plugin credential linking). Body: multipart `file` (+ optional
        `key` naming the schema field). Stores at
        <config>/plugins/<plugin_id>/data/<key> and records the path in the
        plugin's settings under `key` so the auto-generated form shows it."""
        if "file" not in request.files:
            return jsonify({"error": "no file provided"}), 400
        f = request.files["file"]
        if not f.filename:
            return jsonify({"error": "empty filename"}), 400
        key = (request.form.get("key") or "").strip() or f"file_{f.filename}"
        from pathlib import Path as _Path
        data_dir = _Path(manager.plugins_dir) / plugin_id / "data"
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return jsonify({"error": f"cannot create plugin data dir: {e}"}), 500
        dest = data_dir / f.filename
        try:
            f.save(str(dest))
        except OSError as e:
            return jsonify({"error": f"failed to save file: {e}"}), 500
        # Record the stored path in the plugin's settings (per-plugin copy).
        row = db.session.get(PluginSetting, (plugin_id, key))
        if row is None:
            db.session.add(PluginSetting(plugin_id=plugin_id, key=key, value=str(dest)))
        else:
            row.value = str(dest)
        db.session.commit()
        loaded = manager.get_loaded(plugin_id)
        if loaded:
            manager.call_safe(loaded, "on_settings_changed", {key: str(dest)})
        return jsonify({"ok": True, "key": key, "path": str(dest)})

    @bp.route("/<plugin_id>/action/<action_id>", methods=["POST"])
    def run_action(plugin_id, action_id):
        """Run an imperative plugin action declared in the manifest `actions`
        array (Brief 6 §2) — e.g. VPN start/stop. Calls the matching method
        on the plugin instance (action id -> method name, snake_cased)."""
        loaded = manager.get_loaded(plugin_id)
        if not loaded or not loaded.enabled:
            return jsonify({"error": "plugin not loaded/enabled"}), 404
        # Only allow actions the manifest declares.
        declared = {a.get("id") for a in (loaded.manifest.actions or [])}
        if action_id not in declared:
            return jsonify({"error": f"action '{action_id}' not declared by {plugin_id}"}), 400
        method_name = action_id.replace("-", "_")
        method = getattr(loaded.instance, method_name, None)
        if not callable(method):
            return jsonify({"error": f"plugin has no method '{method_name}' for action '{action_id}'"}), 500
        result = manager.call_safe(loaded, method_name)
        # Accept tuple[bool, str] (VPN start/stop) or any JSON-able return.
        if isinstance(result, tuple) and len(result) == 2:
            ok, msg = result
            return jsonify({"success": bool(ok), "message": str(msg)}), (200 if ok else 400)
        return jsonify({"ok": True, "result": result})

    @bp.route("/<plugin_id>/status", methods=["GET"])
    def plugin_status(plugin_id):
        """Live read-only status for plugins that expose a `status()` method
        (e.g. VPN: running, public_ip, handshake)."""
        loaded = manager.get_loaded(plugin_id)
        if not loaded or not loaded.enabled:
            return jsonify({"error": "plugin not loaded/enabled"}), 404
        method = getattr(loaded.instance, "status", None)
        if not callable(method):
            return jsonify({"error": "plugin does not expose status()"}), 404
        result = manager.call_safe(loaded, "status")
        if result is None:
            return jsonify({"error": "status() failed"}), 500
        return jsonify(result)

    @bp.route("/<plugin_id>/settings", methods=["GET"])
    def get_settings(plugin_id):
        rows = PluginSetting.query.filter_by(plugin_id=plugin_id).all()
        from plugins.secret_store import decrypt as _dec
        out = {}
        for r in rows:
            if r.secret:
                try:
                    out[r.key] = _dec(r.value)
                except Exception:
                    out[r.key] = ""  # key rotated/unavailable — re-enter
            else:
                out[r.key] = r.value
        return jsonify(out)

    @bp.route("/<plugin_id>/settings", methods=["POST"])
    def set_settings(plugin_id):
        payload = request.get_json(silent=True) or {}
        loaded = manager.get_loaded(plugin_id)
        schema = (loaded.manifest.settings_schema if loaded else []) or []
        secret_keys = {f.get("key") for f in schema if f.get("type") == "secret"}
        from plugins.secret_store import encrypt as _enc
        for key, value in payload.items():
            secret = key in secret_keys
            stored = _enc(str(value)) if secret else str(value)
            row = db.session.get(PluginSetting, (plugin_id, key))
            if row is None:
                row = PluginSetting(plugin_id=plugin_id, key=key,
                                    value=stored, secret=secret)
                db.session.add(row)
            else:
                row.value = stored
                row.secret = secret
        db.session.commit()

        loaded = manager.get_loaded(plugin_id)
        if loaded:
            manager.call_safe(loaded, "on_settings_changed", payload)
        return jsonify({"ok": True})

    @bp.route("/<plugin_id>/health", methods=["GET"])
    def health(plugin_id):
        row = db.session.get(InstalledPlugin, plugin_id)
        if not row:
            return jsonify({"error": "not installed"}), 404
        return jsonify({
            "consecutive_failures": row.consecutive_failures,
            "last_error": row.last_error,
            "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
            "enabled": row.enabled,
        })

    # -- marketplace / repositories -----------------------------------------

    @bp.route("/marketplace", methods=["GET"])
    def marketplace():
        return jsonify(registry.list_available())

    @bp.route("/install", methods=["POST"])
    def install():
        payload = request.get_json(silent=True) or {}
        plugin_id = payload.get("plugin_id")
        version = payload.get("version")
        if not plugin_id:
            return jsonify({"error": "plugin_id is required"}), 400
        # Reinstalling a user-uninstalled bundled plugin is fine (tombstone
        # gets cleared below); only block installing an ACTIVE bundled id
        # from a repo.
        from models import AppSetting
        tombstone_key = f"plugin.uninstalled.{plugin_id}"
        is_tombstoned = db.session.get(AppSetting, tombstone_key) is not None
        if plugin_id in manager.bundled_ids() and not is_tombstoned:
            return jsonify({"error": "This plugin is bundled with fnack — no need to install it from a repository."}), 400
        try:
            row = registry.install(plugin_id, version)
            # Clear the tombstone so auto-install considers it installed again.
            if is_tombstoned:
                t = db.session.get(AppSetting, tombstone_key)
                if t:
                    db.session.delete(t)
                    db.session.commit()
        except RegistryError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "id": row.id, "version": row.version})

    @bp.route("/repositories", methods=["GET"])
    def list_repositories():
        repos = PluginRepository.query.all()
        return jsonify([
            {"id": r.id, "name": r.name, "url": r.url, "enabled": r.enabled,
             "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None}
            for r in repos
        ])

    @bp.route("/repositories", methods=["POST"])
    def add_repository():
        payload = request.get_json(silent=True) or {}
        url = (payload.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400
        try:
            repo = registry.add_repository(url)
        except RegistryError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "id": repo.id, "name": repo.name})

    @bp.route("/repositories/<int:repo_id>/refresh", methods=["POST"])
    def refresh_repository(repo_id):
        try:
            registry.refresh_repository(repo_id)
        except RegistryError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @bp.route("/repositories/<int:repo_id>", methods=["DELETE"])
    def remove_repository(repo_id):
        registry.remove_repository(repo_id)
        return jsonify({"ok": True})

    # -- config-as-code (Phase 4, PLUGIN_ARCHITECTURE.md §11) ----------------

    _SECRET_KEYS = {"api_key", "token", "webhook_url", "client_secret", "password", "secret"}

    @bp.route("/export", methods=["GET"])
    def export_config():
        """Export full plugin state (repos + installed + settings, secrets
        redacted) as one JSON blob — pairs with the DEPLOY.md move story."""
        repos = [{"url": r.url, "enabled": r.enabled}
                 for r in PluginRepository.query.all()]
        plugins = {}
        for row in InstalledPlugin.query.all():
            settings = {s.key: s.value for s in PluginSetting.query.filter_by(plugin_id=row.id).all()}
            redacted = {
                k: ("<redacted>" if k.lower() in _SECRET_KEYS or "secret" in k.lower() or "token" in k.lower() or "key" in k.lower() else v)
                for k, v in settings.items()
            }
            # Phase 1.1: export capability-specific priority overrides so a
            # full config move preserves them (not just priority_override).
            from plugins.models import PluginCapabilityPriority
            cap_priorities = {
                cp.capability_id: cp.priority
                for cp in PluginCapabilityPriority.query.filter_by(plugin_id=row.id).all()
            }
            plugins[row.id] = {
                "version": row.version,
                "enabled": row.enabled,
                "trust_level": row.trust_level,
                "settings": redacted,
                "priority_override": row.priority_override,
                "capability_priorities": cap_priorities,
            }
        return jsonify({"repos": repos, "plugins": plugins})

    @bp.route("/import", methods=["POST"])
    def import_config():
        """Re-add repos, install pinned versions, restore enabled/settings/
        priority from an exported blob. Secrets are NOT restored (redacted in
        export) — the user must re-enter them."""
        payload = request.get_json(silent=True) or {}
        added, installed = [], []
        try:
            for r in payload.get("repos", []):
                if r.get("url"):
                    repo = registry.add_repository(r["url"])
                    repo.enabled = bool(r.get("enabled", True))
                    added.append(r["url"])
            db.session.commit()
            for pid, meta in (payload.get("plugins") or {}).items():
                version = meta.get("version")
                try:
                    row = registry.install(pid, version)
                    row.enabled = bool(meta.get("enabled", True))
                    if meta.get("priority_override") is not None:
                        row.priority_override = meta["priority_override"]
                    # Phase 1.1: restore capability-specific priority overrides
                    # (written directly — the manager re-registers on next
                    # enable; values are small, non-secret integers).
                    from plugins.models import PluginCapabilityPriority
                    for cap_id, cap_prio in (meta.get("capability_priorities") or {}).items():
                        if cap_prio is None:
                            continue
                        try:
                            value = int(cap_prio)
                        except (TypeError, ValueError):
                            continue
                        if value < 1:
                            continue
                        cp_row = db.session.get(PluginCapabilityPriority, (pid, str(cap_id)))
                        if cp_row is None:
                            db.session.add(PluginCapabilityPriority(
                                plugin_id=pid, capability_id=str(cap_id), priority=value,
                            ))
                        else:
                            cp_row.priority = value
                    for k, v in (meta.get("settings") or {}).items():
                        if v == "<redacted>":
                            continue
                        s = db.session.get(PluginSetting, (pid, k))
                        if s is None:
                            db.session.add(PluginSetting(plugin_id=pid, key=k, value=str(v)))
                        else:
                            s.value = str(v)
                    db.session.commit()
                    installed.append(pid)
                except RegistryError as exc:
                    # Plugin not in any added repo — skip, don't abort the batch.
                    db.session.rollback()
            return jsonify({"ok": True, "repos_added": added, "plugins_installed": installed})
        except Exception as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400

    return bp
