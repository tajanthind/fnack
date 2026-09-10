"""Generic integration API (v1): the stable HTTP boundary for external
systems that need fnack's accounts, library and per-account user state.

This is deliberately protocol-agnostic — it speaks plain JSON and knows
nothing about any particular server or client protocol. A server product
implements its own protocol on top of these primitives; a plugin never needs
to import core internals.

Auth: an account session cookie **or** an account API token
(``Authorization: Bearer fnack_…`` / ``X-API-Token``). ``POST /auth/token``
(credential exchange) and ``GET /meta`` (capability discovery) are the only
public endpoints; every other endpoint requires an account identity, and every user-state
read/write is scoped to that identity — no endpoint accepts an account id
from the caller. The machine-level API key does NOT authenticate here: it
carries no account and could not honour per-account isolation.

Errors: ``400`` invalid request/unknown library id, ``401`` unauthenticated,
``403`` forbidden, ``404`` not found OR not owned by this account (existence
is never leaked across accounts).
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from services import library_query, user_state
from services.accounts import (
    create_api_token,
    current_user,
    list_api_tokens,
    revoke_api_token,
    token_json,
    verify_password,
)

API_NAME = "fnack-integration"
API_VERSION = "v1"
PUBLIC_PATHS = ("/api/integration/v1/auth/token",)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _account():
    """The authenticated account for this request (session or token)."""
    return current_user()


def _error(message, status=400):
    return jsonify({"error": message}), status


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _account_json(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _int_arg(name, default=None):
    raw = request.args.get(name, default)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise user_state.ValidationError(f"{name} must be an integer")


def build_integration_blueprint() -> Blueprint:
    bp = Blueprint("integration_api", __name__, url_prefix="/api/integration/v1")

    @bp.errorhandler(user_state.NotFound)
    def _not_found(exc):
        return _error(str(exc), 404)

    @bp.errorhandler(user_state.ValidationError)
    def _invalid(exc):
        return _error(str(exc), 400)

    @bp.errorhandler(library_query.LibraryQueryError)
    def _bad_query(exc):
        return _error(str(exc), 400)

    # -- meta ---------------------------------------------------------------

    @bp.route("/meta", methods=["GET"])
    def meta():
        return jsonify({
            "name": API_NAME,
            "version": API_VERSION,
            "auth": {
                "types": ["session_cookie", "bearer_token"],
                "token_header": "Authorization: Bearer fnack_…",
                "token_endpoint": PUBLIC_PATHS[0],
            },
            "features": [
                "accounts", "library_search", "library_pagination",
                "playlists", "favorites", "ratings", "bookmarks",
                "scrobble_history", "play_queue",
            ],
        })

    # -- auth ---------------------------------------------------------------

    @bp.route("/auth/token", methods=["POST"])
    def issue_token():
        """Exchange account credentials for an API token (public endpoint)."""
        data = _body()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        user = None
        if username:
            from services.accounts import user_by_username
            user = user_by_username(username)
        if user is None or not verify_password(user, password):
            return _error("invalid username or password", 401)
        try:
            plaintext, row = create_api_token(
                user, label=data.get("label"), expires_in_days=data.get("expires_in_days"))
        except ValueError as exc:
            return _error(str(exc), 400)
        return jsonify({
            "token": plaintext,
            "token_type": "bearer",
            "account": _account_json(user),
            **token_json(row),
        }), 201

    @bp.route("/me", methods=["GET"])
    def me():
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        return jsonify({
            "account": _account_json(user),
            "state": user_state.summary(user.id),
            "library": library_query.stats(),
        })

    @bp.route("/tokens", methods=["GET", "POST"])
    def tokens():
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        if request.method == "GET":
            return jsonify({"items": list_api_tokens(user)})
        data = _body()
        try:
            plaintext, row = create_api_token(
                user, label=data.get("label"), expires_in_days=data.get("expires_in_days"))
        except ValueError as exc:
            return _error(str(exc), 400)
        return jsonify({"token": plaintext, "token_type": "bearer",
                        **token_json(row)}), 201

    @bp.route("/tokens/<int:token_id>", methods=["DELETE"])
    def revoke_token(token_id):
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        if not revoke_api_token(user, token_id):
            return _error("no such token for this account", 404)
        return jsonify({"ok": True})

    # -- library ------------------------------------------------------------

    @bp.route("/library/stats", methods=["GET"])
    def library_stats():
        if _account() is None:
            return _error("authentication required", 401)
        return jsonify(library_query.stats())

    @bp.route("/library/artists", methods=["GET"])
    def library_artists():
        if _account() is None:
            return _error("authentication required", 401)
        return jsonify(library_query.artists(
            q=request.args.get("q"),
            offset=_int_arg("offset", 0), limit=_int_arg("limit", library_query.DEFAULT_PAGE),
            sort=request.args.get("sort", "name"), order=request.args.get("order", "asc")))

    @bp.route("/library/artists/<int:artist_id>", methods=["GET"])
    def library_artist(artist_id):
        if _account() is None:
            return _error("authentication required", 401)
        artist = library_query.get_artist(artist_id)
        if artist is None:
            return _error(f"no artist with id {artist_id}", 404)
        return jsonify(artist)

    @bp.route("/library/albums", methods=["GET"])
    def library_albums():
        if _account() is None:
            return _error("authentication required", 401)
        return jsonify(library_query.albums(
            q=request.args.get("q"), artist_id=_int_arg("artist_id"),
            offset=_int_arg("offset", 0), limit=_int_arg("limit", library_query.DEFAULT_PAGE),
            sort=request.args.get("sort", "name"), order=request.args.get("order", "asc")))

    @bp.route("/library/albums/<int:album_id>", methods=["GET"])
    def library_album(album_id):
        if _account() is None:
            return _error("authentication required", 401)
        album = library_query.get_album(album_id)
        if album is None:
            return _error(f"no album with id {album_id}", 404)
        return jsonify(album)

    @bp.route("/library/tracks", methods=["GET"])
    def library_tracks():
        if _account() is None:
            return _error("authentication required", 401)
        downloaded = request.args.get("downloaded")
        downloaded = None if downloaded is None else downloaded.lower() in ("1", "true", "yes")
        return jsonify(library_query.tracks(
            q=request.args.get("q"), album_id=_int_arg("album_id"),
            artist_id=_int_arg("artist_id"), downloaded=downloaded,
            status=request.args.get("status"),
            offset=_int_arg("offset", 0), limit=_int_arg("limit", library_query.DEFAULT_PAGE),
            sort=request.args.get("sort", "title"), order=request.args.get("order", "asc")))

    @bp.route("/library/tracks/<int:track_id>", methods=["GET"])
    def library_track(track_id):
        if _account() is None:
            return _error("authentication required", 401)
        track = library_query.get_track(track_id)
        if track is None:
            return _error(f"no track with id {track_id}", 404)
        return jsonify(track)

    # -- playlists ----------------------------------------------------------

    @bp.route("/playlists", methods=["GET", "POST"])
    def playlists():
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        if request.method == "GET":
            return jsonify(user_state.list_playlists(
                user.id, offset=_int_arg("offset", 0),
                limit=_int_arg("limit", user_state.DEFAULT_PAGE)))
        data = _body()
        return jsonify(user_state.create_playlist(
            user.id, data.get("name"), comment=data.get("comment"),
            track_ids=data.get("track_ids"))), 201

    @bp.route("/playlists/<int:playlist_id>", methods=["GET", "PATCH", "DELETE"])
    def playlist(playlist_id):
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        if request.method == "GET":
            return jsonify(user_state.get_playlist(
                user.id, playlist_id, offset=_int_arg("offset", 0),
                limit=_int_arg("limit")))
        if request.method == "DELETE":
            user_state.delete_playlist(user.id, playlist_id)
            return jsonify({"ok": True})
        data = _body()
        kwargs = {}
        if "name" in data:
            kwargs["name"] = data["name"]
        if "comment" in data:
            kwargs["comment"] = data["comment"]
        return jsonify(user_state.update_playlist(user.id, playlist_id, **kwargs))

    @bp.route("/playlists/<int:playlist_id>/items", methods=["POST", "PUT"])
    def playlist_items(playlist_id):
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        data = _body()
        if request.method == "PUT":
            # Full reorder: the exact item order for this playlist.
            return jsonify(user_state.reorder_items(user.id, playlist_id,
                                                    data.get("item_ids")))
        return jsonify(user_state.add_tracks(
            user.id, playlist_id, data.get("track_ids"),
            position=data.get("position"))), 201

    @bp.route("/playlists/<int:playlist_id>/items/<int:item_id>", methods=["DELETE"])
    def playlist_item(playlist_id, item_id):
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        return jsonify(user_state.remove_item(user.id, playlist_id, item_id))

    # -- favorites ----------------------------------------------------------

    @bp.route("/favorites", methods=["GET"])
    def favorites():
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        return jsonify(user_state.list_favorites(
            user.id, item_type=request.args.get("type"),
            offset=_int_arg("offset", 0), limit=_int_arg("limit", user_state.DEFAULT_PAGE)))

    @bp.route("/favorites/<item_type>/<int:item_id>", methods=["GET", "PUT", "DELETE"])
    def favorite(item_type, item_id):
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        if request.method == "GET":
            return jsonify({"item_type": item_type, "item_id": item_id,
                            "starred": user_state.is_starred(user.id, item_type, item_id)})
        if request.method == "DELETE":
            removed = user_state.unstar(user.id, item_type, item_id)
            return jsonify({"ok": True, "removed": removed})
        return jsonify(user_state.star(user.id, item_type, item_id))

    # -- ratings ------------------------------------------------------------

    @bp.route("/ratings", methods=["GET"])
    def ratings():
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        return jsonify(user_state.list_ratings(
            user.id, item_type=request.args.get("type"),
            offset=_int_arg("offset", 0), limit=_int_arg("limit", user_state.DEFAULT_PAGE)))

    @bp.route("/ratings/<item_type>/<int:item_id>", methods=["GET", "PUT", "DELETE"])
    def rating(item_type, item_id):
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        if request.method == "GET":
            return jsonify({"item_type": item_type, "item_id": item_id,
                            "rating": user_state.get_rating(user.id, item_type, item_id)})
        if request.method == "DELETE":
            return jsonify(user_state.set_rating(user.id, item_type, item_id, None))
        return jsonify(user_state.set_rating(user.id, item_type, item_id,
                                             _body().get("rating")))

    # -- bookmarks ----------------------------------------------------------

    @bp.route("/bookmarks", methods=["GET"])
    def bookmarks():
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        return jsonify(user_state.list_bookmarks(
            user.id, offset=_int_arg("offset", 0),
            limit=_int_arg("limit", user_state.DEFAULT_PAGE)))

    @bp.route("/bookmarks/<item_type>/<int:item_id>", methods=["GET", "PUT", "DELETE"])
    def bookmark(item_type, item_id):
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        if request.method == "GET":
            bookmark_row = user_state.get_bookmark(user.id, item_type, item_id)
            if bookmark_row is None:
                return _error("no such bookmark", 404)
            return jsonify(bookmark_row)
        if request.method == "DELETE":
            removed = user_state.delete_bookmark(user.id, item_type, item_id)
            return jsonify({"ok": True, "removed": removed})
        data = _body()
        return jsonify(user_state.set_bookmark(user.id, item_type, item_id,
                                               data.get("position_ms"),
                                               comment=data.get("comment")))

    # -- scrobble history ---------------------------------------------------

    @bp.route("/scrobbles", methods=["GET", "POST"])
    def scrobbles():
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        if request.method == "GET":
            return jsonify(user_state.list_scrobbles(
                user.id, offset=_int_arg("offset", 0),
                limit=_int_arg("limit", user_state.DEFAULT_PAGE),
                since=request.args.get("since")))
        data = _body()
        return jsonify(user_state.record_scrobble(
            user.id, data.get("track_id"), played_at=data.get("played_at"),
            submission=data.get("submission", True))), 201

    @bp.route("/scrobbles/top", methods=["GET"])
    def scrobble_top():
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        return jsonify({"items": user_state.play_counts(
            user.id, limit=_int_arg("limit", 50))})

    # -- play queue ---------------------------------------------------------

    @bp.route("/queue", methods=["GET", "PUT", "DELETE"])
    def queue():
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        if request.method == "GET":
            return jsonify(user_state.get_queue(user.id))
        if request.method == "DELETE":
            return jsonify({"ok": True, "cleared": user_state.clear_queue(user.id)})
        data = _body()
        return jsonify(user_state.save_queue(
            user.id, data.get("track_ids"), current_index=data.get("current_index", 0),
            position_ms=data.get("position_ms", 0), changed_by=data.get("changed_by")))

    @bp.route("/state/summary", methods=["GET"])
    def state_summary():
        user = _account()
        if user is None:
            return _error("authentication required", 401)
        return jsonify(user_state.summary(user.id))

    return bp
