"""Encryption-at-rest for plugin secrets (manifest "type": "secret" settings).

Values declared secret in a plugin's settings_schema are stored in the SQLite
DB encrypted with a Fernet key kept OUTSIDE the database, under the config
directory (``plugin_secret.key``, mode 0600). The DB file alone — a backup, a
misconfigured volume mount — no longer leaks webhook URLs / API tokens in
cleartext; an attacker needs the key file too.

The key is created once and reused for the lifetime of the config directory;
losing it means previously-stored secrets cannot be decrypted (they read as
empty and can be re-entered).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("fnack.plugins.secrets")

_KEY_FILE_NAME = "plugin_secret.key"


def _key_path() -> Path:
    config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
    return config_dir / _KEY_FILE_NAME


def _fernet():
    from cryptography.fernet import Fernet

    key_path = _key_path()
    try:
        if not key_path.exists():
            key_path.write_bytes(Fernet.generate_key())
            try:
                key_path.chmod(0o600)
            except OSError:
                pass
        return Fernet(key_path.read_bytes())
    except Exception:
        logger.exception("[SECRETS] Could not initialise plugin secret key at %s", key_path)
        raise


def encrypt(value) -> str:
    """Encrypt a plaintext secret into a Fernet token (str)."""
    return _fernet().encrypt(str(value).encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a Fernet token back to plaintext."""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


def looks_encrypted(value: str) -> bool:
    """True if the value looks like a Fernet token (used for idempotent
    backfill of values stored as plaintext before encryption existed)."""
    if not value or len(value) < 50:
        return False
    try:
        decrypt(value)
        return True
    except Exception:
        return False
