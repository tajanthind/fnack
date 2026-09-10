"""Accounts: first-run setup, login, multi-user with roles.

Industry-standard credential storage (werkzeug scrypt — salted, one-way) and
a whole-app lockdown guard:

* Every request except /health, /static, /login, /logout, /setup and
  /socket.io requires an identity:
    - a logged-in session (User account), OR
    - the optional M2M API key (X-API-Key / Bearer), OR
    - an enabled auth_provider plugin (reverse-proxy SSO etc.).
* If NO user account exists yet (first boot of a fresh volume, or a wiped
  database), fnack is locked down to /setup until an initial ADMIN account
  is created — "setup an account when spinning up the container" is
  enforced, not optional.
* Roles: 'admin' (manage accounts) and 'user' (everything else). Only the
  first account (created via /setup) is admin; admins create more.

The guard + routes live in one module so architecture tests can attach them
to a throwaway Flask app without importing the full `app.py` (which would
boot background workers against the live config DB).

Password storage: werkzeug.security.generate_password_hash — scrypt
(method="scrypt", per-user random salt). Never plaintext, never reversible;
login compares via check_password_hash (constant-time).
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from models import db

# Account API tokens are opaque bearer credentials: `fnack_` + 32 random
# bytes, URL-safe. Only their SHA-256 hash is stored (see ApiToken), so the
# database never contains a usable credential. The prefix is kept for display.
TOKEN_PREFIX = "fnack_"

MIN_PASSWORD_LENGTH = 8
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,64}$")

# Paths that never need an account / session.
# /api/integration/v1/auth/token is the token-exchange endpoint for
# programmatic clients: it authenticates credentials itself and is how a
# client obtains an account token in the first place.
OPEN_PATHS = {"/health", "/login", "/logout", "/setup", "/favicon.ico", "/socket.io",
              "/api/integration/v1/auth/token", "/api/integration/v1/meta"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# --------------------------------------------------------------------------
# model helpers
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """scrypt hash (salted, one-way) — industry-standard storage."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return generate_password_hash(password, method="scrypt")


def verify_password(user, password: str) -> bool:
    from models import User
    if not isinstance(user, User) or not user.password_hash:
        return False
    return check_password_hash(user.password_hash, password)


def user_count() -> int:
    from models import User
    return User.query.count()


def create_user(username: str, password: str, role: str = "user") -> "User":
    """Create a user. The FIRST account ever created is forced to admin."""
    from models import User
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValueError(
            "username must be 3-64 chars: letters, digits, '.', '_', '-'")
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if User.query.filter_by(username=username).first():
        raise ValueError(f"username '{username}' is taken")
    role = "admin" if role == "admin" or user_count() == 0 else "user"
    user = User(username=username, password_hash=hash_password(password), role=role)
    db.session.add(user)
    db.session.commit()
    return user


def user_by_username(username: str):
    from models import User
    return User.query.filter_by(username=(username or "").strip()).first()


def current_user():
    """The logged-in User from the session (cached on g), or None."""
    if hasattr(g, "_fnack_current_user"):
        return g._fnack_current_user
    from models import User
    uid = session.get("uid")
    user = db.session.get(User, uid) if uid else None
    g._fnack_current_user = user
    return user


def is_admin() -> bool:
    user = current_user()
    return bool(user and user.role == "admin")


# --------------------------------------------------------------------------
# account API tokens (programmatic clients)
# --------------------------------------------------------------------------

def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def create_api_token(user, label: str | None = None, expires_in_days=None):
    """Mint a token for ``user``.

    Returns ``(plaintext, row)`` — the plaintext is shown to the caller once
    and never stored (only its hash is), so it cannot be recovered later.
    """
    from models import ApiToken
    if getattr(user, "id", None) is None:
        raise ValueError("a persisted account is required")
    if label is not None:
        label = str(label)[:128] or None
    expires_at = None
    if expires_in_days:
        try:
            days = int(expires_in_days)
        except (TypeError, ValueError):
            raise ValueError("expires_in_days must be an integer")
        if days <= 0:
            raise ValueError("expires_in_days must be > 0")
        expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(
        user_id=user.id,
        label=label,
        prefix=plaintext[: len(TOKEN_PREFIX) + 8],
        token_hash=_hash_token(plaintext),
        expires_at=expires_at,
    )
    db.session.add(row)
    db.session.commit()
    return plaintext, row


def verify_api_token(plaintext: str):
    """Resolve a bearer token to its account, or None.

    Rejects unknown, revoked and expired tokens; records last use. Lookup is
    by hash (unique index) so a database read cannot be replayed as a token.
    """
    from models import ApiToken, User
    if not plaintext or not plaintext.startswith(TOKEN_PREFIX):
        return None
    row = ApiToken.query.filter_by(token_hash=_hash_token(plaintext)).first()
    if row is None or row.revoked_at is not None:
        return None
    now = datetime.now(timezone.utc)
    if row.expires_at is not None:
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            return None
    user = db.session.get(User, row.user_id)
    if user is None:
        return None
    row.last_used_at = now
    db.session.commit()
    return user


def token_json(row) -> dict:
    """Safe listing view of a token (never the secret)."""
    return {
        "id": row.id,
        "label": row.label,
        "prefix": row.prefix,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }


def list_api_tokens(user) -> list:
    """This account's tokens, without secrets."""
    from models import ApiToken
    rows = (ApiToken.query.filter_by(user_id=user.id)
            .order_by(ApiToken.created_at.desc(), ApiToken.id.desc()).all())
    return [token_json(r) for r in rows]


def revoke_api_token(user, token_id) -> bool:
    """Revoke one of THIS account's tokens (another account's id is refused)."""
    from models import ApiToken
    try:
        token_id = int(token_id)
    except (TypeError, ValueError):
        return False
    row = ApiToken.query.filter_by(id=token_id, user_id=user.id).first()
    if row is None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    db.session.commit()
    return True


def bearer_token() -> str | None:
    """The account token presented by this request, if any."""
    header = (request.headers.get("Authorization") or "").strip()
    if header.lower().startswith("bearer "):
        header = header[7:].strip()
    candidate = request.headers.get("X-API-Token") or header or ""
    candidate = candidate.strip()
    return candidate or None


# --------------------------------------------------------------------------
# before_request guard
# --------------------------------------------------------------------------

def auth_guard():
    """Whole-app lockdown (registered as app.before_request).

    Identity precedence: session account > account API token (Bearer /
    X-API-Token) > M2M API key > auth_provider plugins (reverse-proxy SSO).
    When no accounts exist at all, only /setup (+ the always-open paths) is
    reachable until an admin account is created. Cross-site state changes
    (CSRF) are refused for session-authenticated requests.

    An account token authenticates as THAT account (g.current_user +
    g.fnack_account_id), so account-scoped APIs can rely on the identity and
    never accept an account id from the caller.
    """
    path = request.path
    if any(path == p or path.startswith(p + "/") for p in ("/health", "/static")):
        return None
    if path in OPEN_PATHS or path.startswith("/socket.io"):
        return None

    from models import AppSetting, User
    no_accounts = User.query.count() == 0

    # Account API token (programmatic clients) — account-scoped identity.
    # Checked before the machine key because a token IS an account.
    presented = bearer_token()
    if presented and presented.startswith(TOKEN_PREFIX):
        token_user = verify_api_token(presented)
        if token_user is not None:
            g.fnack_user = token_user.username
            g.fnack_auth = "token"
            g.current_user = token_user
            g._fnack_current_user = token_user  # same cache current_user() uses
            g.fnack_account_id = token_user.id
            return None  # not cookie-based: no cross-site request forgery risk

    # Optional M2M API key (machine clients) — same identity as before.
    api_key = request.headers.get("X-API-Key") or (
        request.headers.get("Authorization") or "").replace("Bearer ", "")
    if api_key:
        setting = db.session.get(AppSetting, "api_key")
        if setting and setting.value and api_key == setting.value:
            g.fnack_user = "api-key"
            g.fnack_auth = "apikey"
            return None

    # Session account.
    user = current_user()
    if user is not None:
        g.fnack_user = user.username
        g.fnack_auth = "session"
        g.current_user = user
        g.fnack_account_id = user.id
        if (request.method in UNSAFE_METHODS
                and request.headers.get("Origin")
                and not _same_origin(request.headers.get("Origin"), request.host_url)):
            return jsonify({"error": "Cross-origin request refused (CSRF)"}), 403
        return None

    # Enabled auth_provider plugins (reverse-proxy auth, etc.).
    try:
        from fnack.plugin_api.capabilities import AUTH_PROVIDER
        from plugins.manager import plugin_manager as _pm
        providers = []
        if _pm is not None:
            providers = [h.provider for h in _pm.capability_registry.providers(AUTH_PROVIDER)]
        headers = {k: v for k, v in request.headers.items()}
        for provider in providers:
            try:
                identity = _pm.invoke_provider(provider, "authenticate", headers)
            except Exception:
                continue
            if identity:
                g.fnack_user = identity
                g.fnack_auth = "provider"
                return None
    except Exception:
        pass

    if no_accounts:
        # Fresh install / wiped DB: nothing works until the admin account is
        # created on /setup.
        if request.path.startswith("/api/") or _wants_json():
            return jsonify({
                "error": "No account is configured yet — create the initial "
                         "admin account at /setup first."
            }), 403
        return redirect("/setup")
    if request.path.startswith("/api/") or _wants_json():
        return jsonify({"error": "Unauthorized — log in first"}), 401
    return redirect(f"/login?next={request.full_path.rstrip('?')}")


def _wants_json() -> bool:
    accept = (request.headers.get("Accept") or "")
    return "text/html" not in accept and "application/json" in accept


def _same_origin(origin: str, host_url: str) -> bool:
    try:
        from urllib.parse import urlparse
        o = urlparse(origin)
        h = urlparse(host_url)
        return o.scheme == h.scheme and o.netloc == h.netloc
    except Exception:
        return False


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

def build_accounts_blueprint() -> Blueprint:
    bp = Blueprint("accounts", __name__)

    @bp.route("/setup", methods=["GET", "POST"])
    def setup():
        from models import User
        if request.method == "GET":
            if User.query.count() > 0:
                return redirect("/login")
            return render_template("setup.html"), 200
        # POST: create the first (admin) account.
        data = request.get_json(silent=True) or request.form
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        confirm = data.get("confirm") or ""
        if password != confirm:
            return jsonify({"error": "passwords do not match"}), 400
        try:
            user = create_user(username, password, role="admin")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        _start_session(user)
        if _wants_json() or request.path.startswith("/api/"):
            return jsonify({"ok": True, "username": user.username, "role": user.role})
        return redirect("/")

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        from models import User
        if request.method == "GET":
            if User.query.count() == 0:
                return redirect("/setup")
            return render_template("login.html"), 200
        data = request.get_json(silent=True) or request.form
        user = user_by_username(data.get("username") or "")
        if user is None or not verify_password(user, data.get("password") or ""):
            if _wants_json() or request.path.startswith("/api/"):
                return jsonify({"error": "Invalid username or password"}), 401
            return render_template("login.html", error="Invalid username or password"), 401
        _start_session(user)
        nxt = request.values.get("next") or ""
        if nxt and nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        if _wants_json() or request.path.startswith("/api/"):
            return jsonify({"ok": True, "username": user.username, "role": user.role})
        return redirect("/")

    @bp.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect("/login")

    # -- accounts API (multi-user with roles) ------------------------------

    @bp.route("/api/accounts/me", methods=["GET"])
    def me():
        user = current_user()
        if user is None:
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"username": user.username, "role": user.role})

    @bp.route("/api/accounts/me/password", methods=["POST"])
    def change_my_password():
        user = current_user()
        if user is None:
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        if not verify_password(user, data.get("current_password") or ""):
            return jsonify({"error": "Current password is incorrect"}), 400
        new_password = data.get("new_password") or ""
        if not new_password or len(new_password) < MIN_PASSWORD_LENGTH:
            return jsonify({"error": f"password must be at least {MIN_PASSWORD_LENGTH} characters"}), 400
        user.password_hash = hash_password(new_password)
        db.session.commit()
        return jsonify({"ok": True})

    @bp.route("/api/accounts", methods=["GET", "POST"])
    def accounts():
        from models import User
        if not is_admin():
            return jsonify({"error": "Admin access required"}), 403
        if request.method == "GET":
            rows = User.query.order_by(User.id).all()
            return jsonify([{
                "id": u.id, "username": u.username, "role": u.role,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            } for u in rows])
        data = request.get_json(silent=True) or {}
        try:
            user = create_user(data.get("username") or "",
                               data.get("password") or "",
                               role=data.get("role") or "user")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "id": user.id, "username": user.username,
                        "role": user.role}), 201

    @bp.route("/api/accounts/<int:account_id>", methods=["DELETE"])
    def delete_account(account_id):
        from models import User
        if not is_admin():
            return jsonify({"error": "Admin access required"}), 403
        user = db.session.get(User, account_id)
        if user is None:
            return jsonify({"error": "No such account"}), 404
        if current_user() is not None and user.id == current_user().id:
            return jsonify({"error": "You cannot delete your own account"}), 400
        if user.role == "admin" and User.query.filter_by(role="admin").count() <= 1:
            return jsonify({"error": "Cannot delete the last admin account"}), 400
        db.session.delete(user)
        db.session.commit()
        return jsonify({"ok": True})

    @bp.route("/api/accounts/<int:account_id>/role", methods=["POST"])
    def set_role(account_id):
        from models import User
        if not is_admin():
            return jsonify({"error": "Admin access required"}), 403
        user = db.session.get(User, account_id)
        if user is None:
            return jsonify({"error": "No such account"}), 404
        role = (request.get_json(silent=True) or {}).get("role")
        if role not in ("admin", "user"):
            return jsonify({"error": "role must be 'admin' or 'user'"}), 400
        if (user.role == "admin" and role != "admin"
                and User.query.filter_by(role="admin").count() <= 1):
            return jsonify({"error": "Cannot demote the last admin account"}), 400
        user.role = role
        db.session.commit()
        return jsonify({"ok": True})

    return bp


def _start_session(user) -> None:
    session.clear()
    session["uid"] = user.id
    session.permanent = True
