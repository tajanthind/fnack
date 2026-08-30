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


def build_plugins_blueprint(manager: PluginManager, registry: PluginRegistry) -> Blueprint:
    bp = Blueprint("plugins", __name__, url_prefix="/api/plugins")

    # -- installed plugins -------------------------------------------------

    @bp.route("", methods=["GET"])
    def list_installed():
        return jsonify(manager.list_loaded())

    @bp.route("/<plugin_id>/enable", methods=["POST"])
    def enable(plugin_id):
        manager.enable_plugin(plugin_id)
        row = db.session.get(InstalledPlugin, plugin_id)
        if row:
            row.enabled = True
            db.session.commit()
        return jsonify({"ok": True})

    @bp.route("/<plugin_id>/disable", methods=["POST"])
    def disable(plugin_id):
        manager.disable_plugin(plugin_id)
        row = db.session.get(InstalledPlugin, plugin_id)
        if row:
            row.enabled = False
            db.session.commit()
        return jsonify({"ok": True})

    @bp.route("/<plugin_id>/uninstall", methods=["POST"])
    def uninstall(plugin_id):
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

    return bp
