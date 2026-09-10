"""Architecture tests: generic accounts, per-account user state, and the
provider-neutral integration API.

Covers the contract the core must honour for any external server/client
integration (see docs/integration-api.md):

* accounts authenticate independently, programmatically (hashed API tokens)
  and via session;
* every user-state object is owned by exactly one account and reads/writes are
  scoped to the authenticated account — cross-account access is refused;
* playlist ordering is deterministic, positions are unique per owner, and
  reordering can never leave two items on the same position;
* favorites, ratings, bookmarks, scrobble history and the saved queue work;
* state references fnack's internal artist/album/track ids, survives library
  metadata/provider changes, survives hard deletion of the referenced row
  (snapshot + ISRC re-link), and provider identities stay opaque;
* library queries are provider-neutral and paginated in SQL;
* the new tables are created additively on an existing installation;
* core carries no client- or product-specific terminology.

Run from the repo root:

    .venv/bin/python tests/architecture/test_user_state_and_integration_api.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

_COUNTER = {"n": 0}


def _make_app():
    """Throwaway app with the real guard, accounts blueprint and integration
    API wired to an in-memory database."""
    from flask import Flask
    from models import db

    _COUNTER["n"] += 1
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///file:fnack_userstate_{_COUNTER['n']}"
        "?mode=memory&cache=shared&uri=true")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    from services.accounts import auth_guard, build_accounts_blueprint
    from services.integration_api import build_integration_blueprint
    app.before_request(auth_guard)
    app.register_blueprint(build_accounts_blueprint())
    app.register_blueprint(build_integration_blueprint())
    return app, db


def _tables(db):
    import models  # noqa: F401 — registers every core table
    db.create_all()


def _two_accounts():
    from services.accounts import create_user
    return create_user("alice", "alice-pass-123", role="user"), \
        create_user("bob", "bob-pass-1234", role="user")


def _seed_library(artist_count=1, albums_per_artist=1, tracks_per_album=2,
                  prefix="Lib", provider="community.test-provider"):
    """Create a small library and return its internal ids."""
    from models import Album, Artist, Track, db
    artist_ids, album_ids, track_ids = [], [], []
    for a in range(artist_count):
        artist = Artist(name=f"{prefix} Artist {a}", external_id=f"a{a}",
                        provider_id=provider)
        db.session.add(artist)
        db.session.flush()
        artist_ids.append(artist.id)
        for b in range(albums_per_artist):
            album = Album(artist_id=artist.id, name=f"{prefix} Album {a}-{b}", year=2020 + b,
                          external_id=f"al{a}-{b}", provider_id=provider)
            db.session.add(album)
            db.session.flush()
            album_ids.append(album.id)
            for t in range(tracks_per_album):
                track = Track(album_id=album.id, artist_id=artist.id,
                              title=f"{prefix} Track {a}-{b}-{t}",
                              track_number=t + 1, duration=180.0 + t,
                              external_id=f"t{a}-{b}-{t}", provider_id=provider,
                              isrc=f"ISRC{a}{b}{t:04d}", status="missing")
                db.session.add(track)
                db.session.flush()
                track_ids.append(track.id)
    db.session.commit()
    return {"artists": artist_ids, "albums": album_ids, "tracks": track_ids}


# ---------------------------------------------------------------------------
# accounts + credentials
# ---------------------------------------------------------------------------

def test_accounts_authenticate_independently_with_hashed_tokens() -> None:
    from services import user_state
    from services.accounts import (create_api_token, list_api_tokens, revoke_api_token,
                                   user_by_username, verify_api_token)
    app, db = _make_app()
    with app.app_context():
        _tables(db)
        alice, bob = _two_accounts()

        plaintext, row = create_api_token(alice, label="navmuse-sync")
        assert plaintext.startswith("fnack_")
        # The secret is never stored: only a hash + display prefix.
        from models import ApiToken
        stored = db.session.get(ApiToken, row.id)
        assert stored.token_hash != plaintext
        assert plaintext not in stored.token_hash
        assert stored.prefix in plaintext
        assert list_api_tokens(alice)[0]["label"] == "navmuse-sync"
        assert "token" not in list_api_tokens(alice)[0]

        # Each account resolves to itself, independently.
        assert verify_api_token(plaintext).id == alice.id
        bob_token, _ = create_api_token(bob)
        assert verify_api_token(bob_token).id == bob.id
        assert verify_api_token("fnack_not-a-real-token") is None

        # Revocation is account-scoped: bob cannot revoke alice's token.
        assert revoke_api_token(bob, row.id) is False
        assert verify_api_token(plaintext) is not None
        assert revoke_api_token(alice, row.id) is True
        assert verify_api_token(plaintext) is None

        # Usernames/passwords are independent per account.
        from services.accounts import verify_password
        assert verify_password(user_by_username("alice"), "alice-pass-123")
        assert not verify_password(user_by_username("alice"), "bob-pass-1234")
        assert user_state.summary(alice.id)["playlists"] == 0


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------

def test_two_accounts_have_fully_isolated_state() -> None:
    from services import user_state
    app, db = _make_app()
    with app.app_context():
        _tables(db)
        alice, bob = _two_accounts()
        lib = _seed_library()
        track_id = lib["tracks"][0]

        alice_playlist = user_state.create_playlist(alice.id, "Alice mix", track_ids=[track_id])
        user_state.star(alice.id, "track", track_id)
        user_state.set_rating(alice.id, "track", track_id, 5)
        user_state.set_bookmark(alice.id, "track", track_id, 42_000, comment="alice")
        user_state.record_scrobble(alice.id, track_id)
        user_state.save_queue(alice.id, [track_id], changed_by="alice-client")

        # Bob sees nothing of Alice's.
        assert user_state.list_playlists(bob.id)["total"] == 0
        assert user_state.list_favorites(bob.id)["total"] == 0
        assert user_state.list_ratings(bob.id)["total"] == 0
        assert user_state.list_bookmarks(bob.id)["total"] == 0
        assert user_state.list_scrobbles(bob.id)["total"] == 0
        assert user_state.get_queue(bob.id)["items"] == []
        assert user_state.is_starred(bob.id, "track", track_id) is False

        # …and cannot reach them by id either (NotFound, not 403: existence of
        # another account's object is never revealed).
        for call in (
            lambda: user_state.get_playlist(bob.id, alice_playlist["id"]),
            lambda: user_state.update_playlist(bob.id, alice_playlist["id"], name="hijack"),
            lambda: user_state.delete_playlist(bob.id, alice_playlist["id"]),
            lambda: user_state.add_tracks(bob.id, alice_playlist["id"], [track_id]),
            lambda: user_state.reorder_items(bob.id, alice_playlist["id"], []),
        ):
            try:
                call()
                raise AssertionError("cross-account playlist access must be refused")
            except user_state.NotFound:
                pass

        # Alice's state is untouched by Bob's attempts.
        assert user_state.get_playlist(alice.id, alice_playlist["id"])["name"] == "Alice mix"
        assert user_state.get_queue(alice.id)["total"] == 1


# ---------------------------------------------------------------------------
# playlists
# ---------------------------------------------------------------------------

def test_playlist_crud_and_deterministic_ordering() -> None:
    from models import PlaylistItem, db
    from services import user_state
    app, db = _make_app()
    with app.app_context():
        _tables(db)
        alice, _bob = _two_accounts()
        lib = _seed_library(tracks_per_album=4)
        t = lib["tracks"]

        playlist = user_state.create_playlist(alice.id, "Ordered")
        pid = playlist["id"]
        assert playlist["items"] == []

        user_state.add_tracks(alice.id, pid, [t[0], t[1], t[2]])
        got = user_state.get_playlist(alice.id, pid)
        assert [i["track_id"] for i in got["items"]] == [t[0], t[1], t[2]]
        assert [i["position"] for i in got["items"]] == [0, 1, 2]
        item_ids = [i["item_id"] for i in got["items"]]

        # Insert at a position -> deterministic, compacted ordering.
        user_state.add_tracks(alice.id, pid, [t[3]], position=1)
        got = user_state.get_playlist(alice.id, pid)
        assert [i["track_id"] for i in got["items"]] == [t[0], t[3], t[1], t[2]]
        assert [i["position"] for i in got["items"]] == [0, 1, 2, 3]

        # Reorder: exact item set required, duplicates/extras refused.
        reversed_ids = list(reversed([i["item_id"] for i in got["items"]]))
        user_state.reorder_items(alice.id, pid, reversed_ids)
        got = user_state.get_playlist(alice.id, pid)
        assert [i["position"] for i in got["items"]] == [0, 1, 2, 3]
        for bad in ([reversed_ids[0], reversed_ids[0], *reversed_ids[2:]],
                    reversed_ids[:-1], [*reversed_ids, 99999]):
            try:
                user_state.reorder_items(alice.id, pid, bad)
                raise AssertionError("invalid order must be refused")
            except user_state.ValidationError:
                pass

        # Positions stay unique per playlist in the database itself.
        positions = [row[0] for row in db.session.query(PlaylistItem.position)
                     .filter_by(playlist_id=pid).all()]
        assert sorted(positions) == list(range(len(positions)))

        # Removing re-compacts; replacing rewrites in order.
        user_state.remove_item(alice.id, pid, got["items"][0]["item_id"])
        got = user_state.get_playlist(alice.id, pid)
        assert [i["position"] for i in got["items"]] == list(range(got["total"]))
        user_state.set_items(alice.id, pid, [t[3], t[0]])
        got = user_state.get_playlist(alice.id, pid)
        assert [i["track_id"] for i in got["items"]] == [t[3], t[0]]

        # Rename + delete.
        assert user_state.update_playlist(alice.id, pid, name="Renamed")["name"] == "Renamed"
        assert user_state.delete_playlist(alice.id, pid) is True
        assert user_state.list_playlists(alice.id)["total"] == 0

        # Unknown library ids are refused before anything is persisted.
        try:
            user_state.add_tracks(alice.id, user_state.create_playlist(alice.id, "x")["id"],
                                  [999999])
            raise AssertionError("unknown track id must be refused")
        except user_state.ValidationError:
            pass


# ---------------------------------------------------------------------------
# favorites / ratings / bookmarks / scrobbles / queue
# ---------------------------------------------------------------------------

def test_favorites_ratings_bookmarks_scrobbles_and_queue() -> None:
    from services import user_state
    app, db = _make_app()
    with app.app_context():
        _tables(db)
        alice, _bob = _two_accounts()
        lib = _seed_library()
        artist_id, album_id, track_id = lib["artists"][0], lib["albums"][0], lib["tracks"][0]

        # favorites (stars) across object types, idempotent
        user_state.star(alice.id, "artist", artist_id)
        user_state.star(alice.id, "album", album_id)
        user_state.star(alice.id, "track", track_id)
        user_state.star(alice.id, "track", track_id)
        assert user_state.list_favorites(alice.id)["total"] == 3
        assert user_state.list_favorites(alice.id, item_type="track")["total"] == 1
        assert user_state.is_starred(alice.id, "track", track_id) is True
        assert user_state.unstar(alice.id, "track", track_id) is True
        assert user_state.unstar(alice.id, "track", track_id) is False
        assert user_state.list_favorites(alice.id)["total"] == 2

        # ratings 1..5, cleared with None, validated
        assert user_state.set_rating(alice.id, "track", track_id, 4)["rating"] == 4
        assert user_state.get_rating(alice.id, "track", track_id) == 4
        assert user_state.set_rating(alice.id, "album", album_id, 5)["rating"] == 5
        assert user_state.list_ratings(alice.id)["total"] == 2
        assert user_state.set_rating(alice.id, "track", track_id, None)["rating"] is None
        assert user_state.get_rating(alice.id, "track", track_id) is None
        for bad in (0 - 1, 6, "loud"):
            try:
                user_state.set_rating(alice.id, "track", track_id, bad)
                raise AssertionError(f"rating {bad!r} must be refused")
            except user_state.ValidationError:
                pass
        try:
            user_state.set_rating(alice.id, "playlist", 1, 3)
            raise AssertionError("unsupported item type must be refused")
        except user_state.ValidationError:
            pass

        # bookmarks
        bm = user_state.set_bookmark(alice.id, "track", track_id, 61_500, comment="chapter 2")
        assert bm["position_ms"] == 61_500 and bm["comment"] == "chapter 2"
        user_state.set_bookmark(alice.id, "track", track_id, 62_000)
        assert user_state.get_bookmark(alice.id, "track", track_id)["position_ms"] == 62_000
        assert user_state.list_bookmarks(alice.id)["total"] == 1
        assert user_state.delete_bookmark(alice.id, "track", track_id) is True
        assert user_state.get_bookmark(alice.id, "track", track_id) is None
        try:
            user_state.set_bookmark(alice.id, "artist", artist_id, 10)
            raise AssertionError("bookmarks do not apply to artists")
        except user_state.ValidationError:
            pass

        # scrobble history + most-played
        for _ in range(3):
            user_state.record_scrobble(alice.id, track_id)
        user_state.record_scrobble(alice.id, lib["tracks"][1], submission=False)
        assert user_state.list_scrobbles(alice.id)["total"] == 4
        assert user_state.list_scrobbles(alice.id, limit=2)["items"][0]["submission"] is False
        top = user_state.play_counts(alice.id)
        assert top[0]["track_id"] == track_id and top[0]["plays"] == 3

        # saved queue with cursor, replaced atomically
        queue = user_state.save_queue(alice.id, [track_id, lib["tracks"][1]],
                                      current_index=1, position_ms=1_500,
                                      changed_by="client-x")
        assert queue["total"] == 2 and queue["current_index"] == 1
        assert queue["position_ms"] == 1_500 and queue["changed_by"] == "client-x"
        assert [i["position"] for i in queue["items"]] == [0, 1]
        # duplicate tracks are allowed; positions stay unique
        queue = user_state.save_queue(alice.id, [track_id, track_id], current_index=5)
        assert queue["total"] == 2 and queue["current_index"] == 1
        assert user_state.clear_queue(alice.id) is True
        assert user_state.get_queue(alice.id)["items"] == []

        # summary reflects everything the account owns
        summary = user_state.summary(alice.id)
        assert summary["favorites"] == 2 and summary["ratings"] == 1
        assert summary["scrobbles"] == 4 and summary["queue_items"] == 0


# ---------------------------------------------------------------------------
# library churn
# ---------------------------------------------------------------------------

def test_state_survives_track_deletion_and_relinks_by_isrc() -> None:
    from models import Track, db
    from services import user_state
    app, db = _make_app()
    with app.app_context():
        _tables(db)
        alice, bob = _two_accounts()
        lib = _seed_library()
        track_id = lib["tracks"][0]
        album_id = lib["albums"][0]

        playlist = user_state.create_playlist(alice.id, "Keep me", track_ids=[track_id])
        user_state.star(alice.id, "track", track_id)
        user_state.set_rating(alice.id, "track", track_id, 5)
        user_state.set_bookmark(alice.id, "track", track_id, 12_000)
        user_state.record_scrobble(alice.id, track_id)
        user_state.save_queue(alice.id, [track_id])
        user_state.star(bob.id, "track", track_id)

        # Hard-delete the library row the way a sync prune / artist delete does:
        # snapshot first, then remove.
        user_state.snapshot_referenced_tracks([track_id])
        isrc = db.session.get(Track, track_id).isrc
        db.session.delete(db.session.get(Track, track_id))
        db.session.commit()

        # Nothing was lost; entries stay readable and are marked unavailable.
        got = user_state.get_playlist(alice.id, playlist["id"])
        assert got["items"][0]["available"] is False
        assert got["items"][0]["title"] == "Lib Track 0-0-0"
        assert got["items"][0]["track_id"] == track_id  # internal id retained
        favs = user_state.list_favorites(alice.id)["items"]
        assert favs[0]["available"] is False and favs[0]["isrc"] == isrc
        assert user_state.list_scrobbles(alice.id)["items"][0]["title"] == "Lib Track 0-0-0"
        assert user_state.get_queue(alice.id)["items"][0]["track_id"] == track_id

        # The provider re-creates the same recording under a NEW internal id.
        replacement = Track(album_id=album_id, artist_id=lib["artists"][0],
                            title="Lib Track 0-0-0", track_number=1, duration=180.0,
                            isrc=isrc, provider_id="community.other-provider",
                            external_id="replacement-1", status="missing")
        db.session.add(replacement)
        db.session.commit()
        assert replacement.id != track_id
        relinked = user_state.relink_tracks([replacement.id])
        assert relinked >= 5, f"expected every state row to re-attach, got {relinked}"

        got = user_state.get_playlist(alice.id, playlist["id"])
        assert got["items"][0]["track_id"] == replacement.id
        assert got["items"][0]["available"] is True
        assert user_state.get_rating(alice.id, "track", replacement.id) == 5
        assert user_state.get_bookmark(alice.id, "track", replacement.id)["position_ms"] == 12_000
        assert user_state.is_starred(alice.id, "track", replacement.id) is True
        assert user_state.get_queue(alice.id)["items"][0]["track_id"] == replacement.id
        assert user_state.list_scrobbles(alice.id)["items"][0]["track_id"] == replacement.id
        # Bob's own star survived too (state is per-account, not global).
        assert user_state.is_starred(bob.id, "track", replacement.id) is True


def test_provider_identities_stay_opaque_and_separate() -> None:
    from models import Track, db
    from services import library_query, user_state
    app, db = _make_app()
    with app.app_context():
        _tables(db)
        alice, _bob = _two_accounts()
        lib = _seed_library(provider="community.first")
        track_id = lib["tracks"][0]
        playlist = user_state.create_playlist(alice.id, "P", track_ids=[track_id])
        user_state.star(alice.id, "track", track_id)

        # Provider identities are opaque, separately scoped values — never a
        # primary reference for user state.
        row = library_query.get_track(track_id)
        assert row["provider_id"] == "community.first" and row["external_id"] == "t0-0-0"
        result = library_query.tracks(provider_never_used=None) if False else None
        track = db.session.get(Track, track_id)
        track.provider_id = "community.replacement"
        track.external_id = "different-id"
        track.title = "Renamed by provider"
        db.session.commit()

        # State is keyed on the internal id, so provider/metadata churn is
        # invisible to it (the live title simply wins on read).
        got = user_state.get_playlist(alice.id, playlist["id"])
        assert got["items"][0]["track_id"] == track_id
        assert got["items"][0]["title"] == "Renamed by provider"
        assert user_state.is_starred(alice.id, "track", track_id) is True
        assert result is None

        # The user-state and library-query services name no provider.
        for module in ("services/user_state.py", "services/library_query.py",
                       "services/integration_api.py"):
            source = (ROOT / module).read_text(encoding="utf-8").lower()
            for provider in ("deezer", "spotify", "musicbrainz", "itunes", "acoustid",
                             "navidrome", "subsonic"):
                assert provider not in source, f"{module} must stay provider-neutral ({provider!r})"


# ---------------------------------------------------------------------------
# library queries
# ---------------------------------------------------------------------------

def test_paginated_provider_neutral_library_queries() -> None:
    from services import library_query
    app, db = _make_app()
    with app.app_context():
        _tables(db)
        lib = _seed_library(artist_count=7, albums_per_artist=2, tracks_per_album=3,
                            prefix="Zeta")

        # Pagination is bounded and reports totals without loading the library.
        page1 = library_query.tracks(limit=5, offset=0)
        assert page1["total"] == 7 * 2 * 3
        assert len(page1["items"]) == 5
        assert page1["has_more"] is True
        page2 = library_query.tracks(limit=5, offset=5)
        assert {i["id"] for i in page1["items"]}.isdisjoint({i["id"] for i in page2["items"]})
        last = library_query.tracks(limit=5, offset=40)
        assert last["has_more"] is False and len(last["items"]) == 2

        # Sorting + order are whitelisted.
        titles_desc = [i["title"] for i in library_query.tracks(limit=3, sort="title",
                                                               order="desc")["items"]]
        assert titles_desc == sorted(titles_desc, reverse=True)
        for bad in ({"sort": "title; DROP TABLE tracks"}, {"order": "sideways"}):
            try:
                library_query.tracks(**bad)
                raise AssertionError(f"{bad} must be refused")
            except library_query.LibraryQueryError:
                pass

        # Search across track/album/artist level.
        assert library_query.tracks(q="Zeta Track 1-")["total"] == 6
        assert library_query.albums(q="Zeta Album 2")["total"] == 2
        assert library_query.artists(q="Zeta Artist 3")["total"] == 1
        assert library_query.tracks(q="Zeta Artist 0")["total"] == 6  # matches by artist name

        # Filters + direct lookup + counts.
        album_id = lib["albums"][0]
        assert library_query.tracks(album_id=album_id)["total"] == 3
        assert library_query.albums(artist_id=lib["artists"][0])["total"] == 2
        assert library_query.tracks(downloaded=True)["total"] == 0
        assert library_query.get_album(album_id)["track_count"] == 3
        assert library_query.get_artist(lib["artists"][0])["album_count"] == 2
        assert library_query.get_track(lib["tracks"][0])["id"] == lib["tracks"][0]
        assert library_query.get_track(999999) is None
        stats = library_query.stats()
        assert stats["artists"] == 7 and stats["tracks"] == 42


# ---------------------------------------------------------------------------
# HTTP boundary
# ---------------------------------------------------------------------------

def test_integration_api_end_to_end_and_cross_account_refusal() -> None:
    app, db = _make_app()
    with app.app_context():
        _tables(db)
        from models import AppSetting
        _two_accounts()
        lib = _seed_library()
        track_id = lib["tracks"][0]
        db.session.add(AppSetting(key="api_key", value="machine-key-123"))
        db.session.commit()

    client = app.test_client()

    # meta is public; everything else needs an account.
    assert client.get("/api/integration/v1/meta").status_code == 200
    assert client.get("/api/integration/v1/me").status_code == 401
    assert client.get("/api/integration/v1/library/tracks").status_code == 401
    assert client.get("/api/integration/v1/playlists").status_code == 401

    # Token exchange (public endpoint) — wrong password refused.
    assert client.post("/api/integration/v1/auth/token", json={
        "username": "alice", "password": "nope"}).status_code == 401
    alice_token = client.post("/api/integration/v1/auth/token", json={
        "username": "alice", "password": "alice-pass-123", "label": "test"}).get_json()["token"]
    bob_token = client.post("/api/integration/v1/auth/token", json={
        "username": "bob", "password": "bob-pass-1234"}).get_json()["token"]
    alice = {"Authorization": f"Bearer {alice_token}"}
    bob = {"Authorization": f"Bearer {bob_token}"}

    me = client.get("/api/integration/v1/me", headers=alice).get_json()
    assert me["account"]["username"] == "alice"
    assert me["library"]["tracks"] >= 1

    # Library access over HTTP.
    tracks = client.get("/api/integration/v1/library/tracks?limit=1", headers=alice).get_json()
    assert tracks["total"] >= 1 and len(tracks["items"]) == 1
    assert client.get(f"/api/integration/v1/library/tracks/{track_id}",
                      headers=alice).status_code == 200
    assert client.get("/api/integration/v1/library/tracks/999999",
                      headers=alice).status_code == 404

    # Playlist CRUD over HTTP, scoped to the token's account.
    created = client.post("/api/integration/v1/playlists", headers=alice,
                          json={"name": "From HTTP", "track_ids": [track_id]})
    assert created.status_code == 201
    playlist_id = created.get_json()["id"]
    assert client.get("/api/integration/v1/playlists", headers=alice).get_json()["total"] == 1

    # Cross-account access is refused at the HTTP layer too.
    assert client.get(f"/api/integration/v1/playlists/{playlist_id}",
                      headers=bob).status_code == 404
    assert client.delete(f"/api/integration/v1/playlists/{playlist_id}",
                         headers=bob).status_code == 404
    assert client.post(f"/api/integration/v1/playlists/{playlist_id}/items", headers=bob,
                       json={"track_ids": [track_id]}).status_code == 404
    assert client.get("/api/integration/v1/playlists", headers=bob).get_json()["total"] == 0

    # State endpoints round-trip.
    assert client.put(f"/api/integration/v1/favorites/track/{track_id}",
                      headers=alice).status_code == 200
    assert client.get("/api/integration/v1/favorites", headers=alice).get_json()["total"] == 1
    assert client.put(f"/api/integration/v1/ratings/track/{track_id}", headers=alice,
                      json={"rating": 3}).get_json()["rating"] == 3
    assert client.put(f"/api/integration/v1/bookmarks/track/{track_id}", headers=alice,
                      json={"position_ms": 5000}).status_code == 200
    assert client.post("/api/integration/v1/scrobbles", headers=alice,
                       json={"track_id": track_id}).status_code == 201
    assert client.put("/api/integration/v1/queue", headers=alice,
                      json={"track_ids": [track_id], "current_index": 0,
                            "changed_by": "http-test"}).get_json()["total"] == 1
    summary = client.get("/api/integration/v1/state/summary", headers=alice).get_json()
    assert summary == {"playlists": 1, "favorites": 1, "ratings": 1, "bookmarks": 1,
                       "scrobbles": 1, "queue_items": 1}

    # Unknown library ids are rejected with 400 (validated before persistence).
    assert client.post("/api/integration/v1/playlists", headers=alice,
                       json={"name": "bad", "track_ids": [999999]}).status_code == 400

    # The machine API key carries no account, so it cannot reach account state.
    machine = {"X-API-Key": "machine-key-123"}
    assert client.get("/api/integration/v1/me", headers=machine).status_code == 401
    assert client.get("/api/integration/v1/playlists", headers=machine).status_code == 401

    # Session login (browser) sees the same account state as its token.
    session_client = app.test_client()
    assert session_client.post("/login", data={
        "username": "alice", "password": "alice-pass-123"}).status_code == 302
    assert session_client.get("/api/integration/v1/playlists").get_json()["total"] == 1

    # Token listing + revocation are account-scoped.
    tokens = client.get("/api/integration/v1/tokens", headers=alice).get_json()["items"]
    assert len(tokens) == 1 and "token" not in tokens[0]
    bob_tokens = client.get("/api/integration/v1/tokens", headers=bob).get_json()["items"]
    assert client.delete(f"/api/integration/v1/tokens/{tokens[0]['id']}",
                         headers=bob).status_code == 404
    assert client.delete(f"/api/integration/v1/tokens/{bob_tokens[0]['id']}",
                         headers=bob).status_code == 200
    assert client.delete(f"/api/integration/v1/tokens/{tokens[0]['id']}",
                         headers=alice).status_code == 200
    assert client.get("/api/integration/v1/me", headers=alice).status_code == 401


# ---------------------------------------------------------------------------
# migrations + terminology
# ---------------------------------------------------------------------------

def test_migration_creates_user_state_tables_on_existing_install() -> None:
    from models import Album, Artist, Track, db
    from services.schema_migrations import run_schema_migrations
    app, db = _make_app()
    with app.app_context():
        _tables(db)
        # Simulate an installation from before this feature: library + account
        # exist, the new tables do not.
        alice, _bob = _two_accounts()
        lib = _seed_library()
        new_tables = ["api_tokens", "playlists", "playlist_items", "favorites",
                      "ratings", "bookmarks", "scrobbles", "play_queues",
                      "play_queue_entries"]
        for table in new_tables:
            db.session.execute(db.text(f"DROP TABLE IF EXISTS {table}"))
        db.session.commit()
        existing_tracks = Track.query.count()

        run_schema_migrations(engine=db.engine)

        from sqlalchemy import inspect
        present = set(inspect(db.engine).get_table_names())
        missing = [t for t in new_tables if t not in present]
        assert not missing, f"migration must create {missing}"

        # Existing data is intact and usable.
        assert Track.query.count() == existing_tracks
        assert Artist.query.count() == lib["artists"].__len__()
        assert Album.query.count() == lib["albums"].__len__()
        from services import user_state
        assert user_state.list_playlists(alice.id)["total"] == 0
        assert user_state.create_playlist(alice.id, "post-migration")["name"] == "post-migration"


def test_core_carries_no_client_or_product_specific_terminology() -> None:
    """fnack core stays protocol-agnostic: no server/client product names, no
    protocol vocabulary. (plugins/essential.py is allow-listed: it records the
    historical removal of a plugin by name; wayfinder/ is historical records.)"""
    forbidden = ("subsonic", "arpeggi", "navmuse", "audiomuse", "symfonium",
                 "feishin", "dsub", "opensubsonic")
    # plugins/essential.py is allow-listed: it records the historical removal
    # of a plugin by name (catalog history, not core vocabulary).
    allowed = {ROOT / "plugins" / "essential.py"}
    files = [ROOT / "app.py", ROOT / "models.py"]
    files += sorted((ROOT / "services").glob("*.py"))
    files += sorted((ROOT / "plugins").glob("*.py"))
    files += [ROOT / "docs" / "architecture.md", ROOT / "docs" / "integration-api.md"]
    files = [f for f in files if f not in allowed]
    for path in files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8").lower()
        for term in forbidden:
            assert term not in text, f"{path.relative_to(ROOT)} must not contain {term!r}"


if __name__ == "__main__":
    test_accounts_authenticate_independently_with_hashed_tokens()
    test_two_accounts_have_fully_isolated_state()
    test_playlist_crud_and_deterministic_ordering()
    test_favorites_ratings_bookmarks_scrobbles_and_queue()
    test_state_survives_track_deletion_and_relinks_by_isrc()
    test_provider_identities_stay_opaque_and_separate()
    test_paginated_provider_neutral_library_queries()
    test_integration_api_end_to_end_and_cross_account_refusal()
    test_migration_creates_user_state_tables_on_existing_install()
    test_core_carries_no_client_or_product_specific_terminology()
    print("test_user_state_and_integration_api: PASSED")
