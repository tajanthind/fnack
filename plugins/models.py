"""Plugin-system database tables.

These use the *same* `db = SQLAlchemy()` instance from fnack's existing
`models.py`, so they live in the same SQLite file/WAL setup — no second
database, no extra migration tooling beyond what's already there.
"""

from datetime import datetime, timezone

from models import db


class PluginRepository(db.Model):
    __tablename__ = "plugin_repositories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), nullable=False)
    url = db.Column(db.String(1024), nullable=False, unique=True)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    added_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_synced_at = db.Column(db.DateTime, nullable=True)
    cached_index_json = db.Column(db.Text, nullable=True)  # last successfully fetched index


class InstalledPlugin(db.Model):
    __tablename__ = "installed_plugins"

    id = db.Column(db.String(128), primary_key=True)          # manifest "id" (reverse-DNS)
    name = db.Column(db.String(256), nullable=False)
    version = db.Column(db.String(32), nullable=False)
    type = db.Column(db.String(256), nullable=False)           # comma-joined manifest["type"]
    enabled = db.Column(db.Boolean, default=True, nullable=False, index=True)
    trust_level = db.Column(db.String(16), default="community", nullable=False)
    source_repo_id = db.Column(db.Integer, db.ForeignKey("plugin_repositories.id"), nullable=True)
    manifest_json = db.Column(db.Text, nullable=False)
    installed_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Health / auto-disable bookkeeping (see PluginManager._call_safe)
    consecutive_failures = db.Column(db.Integer, default=0, nullable=False)
    last_error = db.Column(db.Text, nullable=True)
    last_run_at = db.Column(db.DateTime, nullable=True)


class PluginSetting(db.Model):
    __tablename__ = "plugin_settings"

    plugin_id = db.Column(db.String(128), primary_key=True)
    key = db.Column(db.String(128), primary_key=True)
    value = db.Column(db.Text, nullable=True)
