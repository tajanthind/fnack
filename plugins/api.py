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
    loaded = manager._plugins.get(plugin_id)  # noqa: SLF001 - internal, same package
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
        return jsonify(manager.list_loaded())

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
        the manifest priority)."""
        payload = request.get_json(silent=True) or {}
        if "priority" not in payload:
            return jsonify({"error": "priority is required"}), 400
        raw = payload.get("priority")
        value = None if raw is None else int(raw)
        if value is not None and value < 1:
            return jsonify({"error": "priority must be >= 1"}), 400
        row = _ensure_installed_row(manager, plugin_id, enabled=True)
        row.priority_override = value
        db.session.commit()
        loaded = manager._plugins.get(plugin_id)  # noqa: SLF001 - internal, same package
        if loaded:
            loaded.priority_override = value
        return jsonify({"ok": True, "priority": value})

    @bp.route("/<plugin_id>/uninstall", methods=["POST"])
    def uninstall(plugin_id):
        # Bundled plugins ship in the image and auto-install on every boot —
        # uninstalling one would just resurrect it. Disable instead.
        if plugin_id in manager.bundled_ids():
            return jsonify({"error": "Bundled plugins cannot be uninstalled — disable it instead (it auto-installs on boot)."}), 400
        try:
            registry.uninstall(plugin_id)
        except RegistryError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @bp.route("/<plugin_id>/settings", methods=["GET"])
    def get_settings(plugin_id):
        rows = PluginSetting.query.filter_by(plugin_id=plugin_id).all()
        return jsonify({r.key: r.value for r in rows})

    @bp.route("/<plugin_id>/settings", methods=["POST"])
    def set_settings(plugin_id):
        payload = request.get_json(silent=True) or {}
        for key, value in payload.items():
            row = db.session.get(PluginSetting, (plugin_id, key))
            if row is None:
                row = PluginSetting(plugin_id=plugin_id, key=key, value=str(value))
                db.session.add(row)
            else:
                row.value = str(value)
        db.session.commit()

        loaded = manager._plugins.get(plugin_id)  # noqa: SLF001 - internal, same package
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
        if plugin_id in manager.bundled_ids():
            return jsonify({"error": "This plugin is bundled with fnack — no need to install it from a repository."}), 400
        try:
            row = registry.install(plugin_id, version)
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
            plugins[row.id] = {
                "version": row.version,
                "enabled": row.enabled,
                "trust_level": row.trust_level,
                "settings": redacted,
                "priority_override": row.priority_override,
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
