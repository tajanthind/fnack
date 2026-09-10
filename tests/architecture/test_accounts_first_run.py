"""Architecture tests: accounts (first-run setup gate, login, roles, M2M key).

The whole-app lockdown (services/accounts.py) is tested against a throwaway
Flask app — never the full app.py (which would boot background workers
against the live config DB):

1. First-run gate: with NO user accounts, everything except /health, /static,
   /login, /setup is refused; /setup creates the initial ADMIN account.
2. Credential storage: password_hash is a salted scrypt hash, never
   plaintext.
3. Login/logout and the M2M API key both unlock protected routes.
4. Roles: only admins manage accounts; a user can change their own password.

IMPORTANT: requests must be issued OUTSIDE a persistent `app.app_context()`
block — Flask binds `g` to the app context, so holding one open across
requests would leak the per-request current_user cache between requests.
The real app never does that; the test mirrors it.

Run from the repo root:

    .venv/bin/python tests/architecture/test_accounts_first_run.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

_COUNTER = {"n": 0}


def _make_app():
    """A minimal fnack-shaped app: real models + the real accounts guard and
    blueprint, plus a couple of probe routes."""
    from flask import Flask, jsonify
    from models import db

    _COUNTER["n"] += 1
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///file:fnack_accounts_{_COUNTER['n']}"
        "?mode=memory&cache=shared&uri=true")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    @app.route("/health")
    def health():
        return jsonify({"ok": True})

    @app.route("/")
    def home():
        return "home"

    @app.route("/api/ping")
    def ping():
        return jsonify({"pong": True})

    from services.accounts import auth_guard, build_accounts_blueprint
    app.before_request(auth_guard)
    app.register_blueprint(build_accounts_blueprint())
    return app, db


def _seed_key(db, value="m2m-secret-key-1"):
    from models import AppSetting
    db.session.add(AppSetting(key="api_key", value=value))
    db.session.commit()


def _create_tables(db):
    import models  # noqa: F401 — registers User etc.
    db.create_all()


def test_first_run_gate_requires_setup() -> None:
    app, db = _make_app()
    with app.app_context():
        _create_tables(db)
    client = app.test_client()

    # Everything except the open paths is refused while no account exists.
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 302 and "/setup" in r.headers.get("Location", "")
    r = client.get("/api/ping")
    assert r.status_code == 403
    assert "No account is configured" in r.get_json()["error"]
    assert client.get("/health").status_code == 200
    assert client.get("/setup").status_code == 200

    # Create the initial admin account.
    r = client.post("/setup", data={
        "username": "admin",
        "password": "correct-horse-battery",
        "confirm": "correct-horse-battery",
    })
    assert r.status_code == 302  # logged in -> "/"

    with app.app_context():
        from models import User
        user = User.query.filter_by(username="admin").first()
        assert user is not None and user.role == "admin"
        assert user.password_hash != "correct-horse-battery"
        assert user.password_hash.startswith("scrypt:"), "must be a salted scrypt hash"
        assert "correct-horse-battery" not in user.password_hash

    # Session is live: protected pages/API now work.
    assert client.get("/").status_code == 200
    assert client.get("/api/ping").status_code == 200
    # /setup now redirects to /login (an account exists).
    assert client.get("/setup").status_code == 302


def test_login_flow_and_bad_password() -> None:
    app, db = _make_app()
    with app.app_context():
        _create_tables(db)
        from services.accounts import create_user
        create_user("alice", "super-secret-1", role="user")
    client = app.test_client()

    assert client.get("/api/ping").status_code == 401
    # Wrong password.
    r = client.post("/login", data={"username": "alice", "password": "wrong"})
    assert r.status_code == 401
    assert client.get("/api/ping").status_code == 401
    # Right password.
    r = client.post("/login", data={"username": "alice", "password": "super-secret-1"})
    assert r.status_code == 302
    assert client.get("/api/ping").status_code == 200
    # Logout kills the session.
    assert client.get("/logout").status_code == 302
    assert client.get("/api/ping").status_code == 401


def test_m2m_api_key_still_works() -> None:
    app, db = _make_app()
    with app.app_context():
        _create_tables(db)
        _seed_key(db)
        from services.accounts import create_user
        create_user("alice", "super-secret-1", role="user")
    client = app.test_client()

    # No session, correct key -> allowed (machine clients).
    assert client.get("/api/ping",
                      headers={"X-API-Key": "m2m-secret-key-1"}).status_code == 200
    # Wrong key -> refused.
    assert client.get("/api/ping",
                      headers={"X-API-Key": "nope"}).status_code == 401
    # Bearer form also works.
    assert client.get("/api/ping",
                      headers={"Authorization": "Bearer m2m-secret-key-1"}).status_code == 200


def test_roles_admin_only_account_management() -> None:
    app, db = _make_app()
    with app.app_context():
        _create_tables(db)
        from services.accounts import create_user
        create_user("admin", "admin-pass-1", role="admin")
        create_user("bob", "bob-pass-123", role="user")

    # Non-admin: no account management.
    bob = app.test_client()
    assert bob.post("/login", data={
        "username": "bob", "password": "bob-pass-123"}).status_code == 302
    assert bob.get("/api/accounts").status_code == 403
    assert bob.post("/api/accounts", json={
        "username": "eve", "password": "eve-pass-123", "role": "user"
    }).status_code == 403

    # Admin: list/create/delete.
    adm = app.test_client()
    assert adm.post("/login", data={
        "username": "admin", "password": "admin-pass-1"}).status_code == 302
    r = adm.get("/api/accounts")
    assert r.status_code == 200
    usernames = {u["username"] for u in r.get_json()}
    assert usernames == {"admin", "bob"}

    r = adm.post("/api/accounts", json={
        "username": "eve", "password": "eve-pass-123", "role": "user"
    })
    assert r.status_code == 201
    eve_id = r.get_json()["id"]

    with app.app_context():
        from models import User
        admin_id = User.query.filter_by(username="admin").first().id
        # Cannot delete your own account.
        assert adm.delete(f"/api/accounts/{admin_id}").status_code == 400
    # Deleting another account works.
    assert adm.delete(f"/api/accounts/{eve_id}").status_code == 200
    assert all(u["username"] != "eve" for u in adm.get("/api/accounts").get_json())

    # Change own password (admin here); old password stops working.
    r = adm.post("/api/accounts/me/password", json={
        "current_password": "admin-pass-1", "new_password": "admin-pass-2"
    })
    assert r.status_code == 200
    adm2 = app.test_client()
    assert adm2.post("/login", data={
        "username": "admin", "password": "admin-pass-2"}).status_code == 302
    bad = app.test_client()
    assert bad.post("/login", data={
        "username": "admin", "password": "admin-pass-1"}).status_code == 401


def test_csrf_origin_check_for_session_requests() -> None:
    app, db = _make_app()
    with app.app_context():
        _create_tables(db)
        from services.accounts import create_user
        create_user("alice", "super-secret-1", role="user")
    client = app.test_client()
    assert client.post("/login", data={
        "username": "alice", "password": "super-secret-1"}).status_code == 302
    # A cross-origin state change is refused even with a valid session.
    r = client.post("/api/accounts/me/password",
                    json={"current_password": "x", "new_password": "y" * 9},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert "Cross-origin" in r.get_json()["error"]
    # Same-origin (or no Origin for CLI clients) is allowed.
    assert client.post("/api/accounts/me/password",
                       json={"current_password": "super-secret-1",
                             "new_password": "new-secret-99"}).status_code == 200


if __name__ == "__main__":
    test_first_run_gate_requires_setup()
    test_login_flow_and_bad_password()
    test_m2m_api_key_still_works()
    test_roles_admin_only_account_management()
    test_csrf_origin_check_for_session_requests()
    print("test_accounts_first_run: PASSED")
