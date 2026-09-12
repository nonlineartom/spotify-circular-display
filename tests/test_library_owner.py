"""Owner UI and music exploration preserve the device's private boundary."""

import json
import os
import time

import pytest

import server


ALBUM = {"id": "saved-one", "uri": "spotify:album:album-one", "title": "A record", "subtitle": "An artist", "image": "/static/mock-album.svg"}
PLAYLIST = {"id": "playlist-one", "uri": "spotify:playlist:playlist-one", "title": "A playlist"}
TRACK = {"number": 1, "disc": 1, "name": "First track", "duration_ms": 180000, "uri": "spotify:track:track-one"}
REMOTE = {"REMOTE_ADDR": "192.168.1.40"}


def test_config_cache_observes_same_size_edits_with_identical_filesystem_metadata(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"marker":"before"}')
    monkeypatch.setattr(server, "CONFIG_FILE", str(path))
    frozen_stat = path.stat()
    original_stat = os.stat

    def unchanged_metadata(target, *args, **kwargs):
        if os.fspath(target) == str(path):
            return frozen_stat
        return original_stat(target, *args, **kwargs)

    monkeypatch.setattr(server.os, "stat", unchanged_metadata)
    assert server.load_config()["marker"] == "before"
    path.write_text('{"marker":"after!"}')
    assert len(path.read_bytes()) == frozen_stat.st_size
    assert server.load_config()["marker"] == "after!"


def test_config_parse_cache_uses_one_snapshot_and_returns_independent_values(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"nested":{"marker":"original"}}')
    monkeypatch.setattr(server, "CONFIG_FILE", str(path))
    original_read = server._read_config_bytes
    original_normalize = server._normalize_config
    reads = []
    parses = []

    def read_once():
        reads.append(True)
        return original_read()

    def normalize_once(config):
        parses.append(True)
        return original_normalize(config)

    monkeypatch.setattr(server, "_read_config_bytes", read_once)
    monkeypatch.setattr(server, "_normalize_config", normalize_once)
    first = server.load_config()
    first["nested"]["marker"] = "mutated"
    assert server.load_config()["nested"]["marker"] == "original"
    assert len(reads) == 2 and len(parses) == 1


@pytest.mark.parametrize("replacement,state", [(None, "missing"), (b"{", "malformed"), (b"[]", "wrong_type"), (b"x" * (server.MAX_CONFIG_BYTES + 1), "too_large")])
def test_config_cache_does_not_reuse_valid_credentials_after_invalid_read(tmp_path, monkeypatch, replacement, state):
    path = tmp_path / "config.json"
    path.write_text('{"refresh_token":"old-private-token"}')
    monkeypatch.setattr(server, "CONFIG_FILE", str(path))
    assert server.load_config()["refresh_token"] == "old-private-token"
    if replacement is None:
        path.unlink()
    else:
        path.write_bytes(replacement)
    assert server.load_config() == {}
    assert server.config_status()["state"] == state


@pytest.fixture
def library_client(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "public_base_url": "http://localhost",
        "spotify_profiles": {"profiles": {"test-account": {
            "account_id": "test-account", "display_name": "Test listener",
            "refresh_token": "test-refresh-token", "kind": "household",
            "connected_at": time.time(), "expires_at": None,
            "authorized_at": time.time(), "receiver_aliases": ["test-alias"],
            "scopes": ["user-library-read", "playlist-read-private"],
        }}},
        "security": {"owner_token": "test-owner-token", "session_secret": "test-session-secret"},
        "wled": {"enabled": False, "devices": []},
    }))
    monkeypatch.setattr(server, "CONFIG_FILE", str(path))
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("SPOTIFY_REDIRECT_URI", raising=False)
    monkeypatch.delenv("OWNER_TOKEN", raising=False)
    server.app.config.update(TESTING=True, SECRET_KEY="test-session-secret")
    monkeypatch.setattr(server, "_crate_cache", {
        "built_at": time.time(),
        "payload": {"sections": [{"id": "saved", "title": "Your albums", "items": [ALBUM, PLAYLIST]}]},
    })
    monkeypatch.setattr(server, "_crate_caches", {
        "generic": {"built_at": time.time(), "payload": {"sections": []}},
        "profile:test-account": server._crate_cache,
    })
    monkeypatch.setattr(server, "_profile_generations", {})
    monkeypatch.setattr(server, "_receiver_identity", {"alias": "test-alias", "active": True, "epoch": "library-epoch"})
    monkeypatch.setattr(server, "_profile_prune_next", 0)
    monkeypatch.setattr(server, "_user_tokens", {})
    monkeypatch.setattr(server, "_account_generation", 0)
    monkeypatch.setattr(server, "_crate_building", False)
    monkeypatch.setattr(server, "_playlist_cache", {"loaded_at": 0, "items": []})
    monkeypatch.setattr(server, "_user_token", None)
    monkeypatch.setattr(server, "_user_token_expiry", 0)
    monkeypatch.setattr(server, "_user_token_grant_id", None)
    monkeypatch.setattr(server, "_build_crate_payload", lambda *_args: {"sections": []})
    monkeypatch.setattr(server, "lookup_album_tracks", lambda _album: [dict(TRACK)])
    monkeypatch.setattr(server, "lookup_album", lambda _album: {})
    monkeypatch.setattr(server, "read_go_librespot_state", lambda: (True, None))
    monkeypatch.setattr(server._http, "get", lambda *_args, **_kwargs: pytest.fail("unexpected network request"))
    monkeypatch.setattr(server._http, "post", lambda *_args, **_kwargs: pytest.fail("unexpected network request"))
    server._rate_buckets.clear()
    return server.app.test_client(), path


@pytest.mark.parametrize("path", ["/api/owner/overview", "/api/library/item?uri=spotify:album:album-one"])
def test_private_overview_and_library_require_owner(library_client, path):
    web, _ = library_client
    response = web.get(path, environ_base=REMOTE)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert "A record" not in response.get_data(as_text=True)


def test_library_play_requires_owner_and_rejects_cross_origin(library_client, monkeypatch):
    web, _ = library_client
    monkeypatch.setattr(server, "play_uri_local", lambda *_args, **_kwargs: pytest.fail("must not play"))
    assert web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}, environ_base=REMOTE).status_code == 401
    assert web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}, headers={"Origin": "https://unrelated.example"}).status_code == 403


def test_owner_overview_is_readable_and_omits_credentials(library_client):
    web, _ = library_client
    response = web.get("/api/owner/overview")
    data = response.get_json()
    assert response.status_code == 200
    assert data["local_kiosk"] is True
    account = data["account"]
    assert account["connected"] is True and account["kind"] == "household"
    assert account["account_id"] == "test-account" and account["display_name"] == "Test listener"
    assert account["profile_count"] == 1 and account["profile_epoch"] == "library-epoch"
    assert account["expires_at"] is None and account["expired"] is False
    assert data["oauth"]["ready"] is True
    assert data["oauth"]["login_url"] == "http://localhost/login"
    assert data["health"]["receiver"]["label"] == "Ready for Spotify Connect"
    for value in ("test-client-id", "test-client-secret", "test-refresh-token", "test-owner-token", "test-session-secret"):
        assert value not in response.get_data(as_text=True)


def test_public_owner_shell_contains_no_account_or_configuration_data(library_client):
    web, _ = library_client
    response = web.get("/owner", environ_base=REMOTE)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    content = response.get_data(as_text=True)
    for value in ("test-client-id", "test-client-secret", "test-refresh-token", "test-owner-token", "A record"):
        assert value not in content


def test_owner_health_distinguishes_paused_from_playing(library_client, monkeypatch):
    web, _ = library_client
    monkeypatch.setattr(server, "read_go_librespot_state", lambda: (True, {"is_playing": False, "item": {"name": "A private song"}}))
    response = web.get("/api/owner/overview")
    assert response.get_json()["health"]["receiver"]["label"] == "Paused on Pi Display"
    assert "A private song" not in response.get_data(as_text=True)


def test_remote_owner_session_can_browse_after_login(library_client):
    web, _ = library_client
    assert web.post("/api/auth/owner", json={"token": "test-owner-token"}, environ_base=REMOTE).status_code == 200
    overview = web.get("/api/owner/overview", environ_base=REMOTE)
    assert overview.get_json()["local_kiosk"] is False
    assert web.get("/api/library/item", query_string={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}, environ_base=REMOTE).status_code == 200


def test_overview_explains_missing_oauth_and_unavailable_receiver(library_client, monkeypatch):
    web, path = library_client
    path.write_text(json.dumps({"wled": {"enabled": True, "devices": []}}))
    monkeypatch.setattr(server, "read_go_librespot_state", lambda: (False, None))
    data = web.get("/api/owner/overview").get_json()
    assert data["account"]["connected"] is False
    assert data["oauth"]["ready"] is False
    assert data["oauth"]["login_url"] is None
    assert data["oauth"]["reason"]
    assert data["health"]["ok"] is False
    assert data["health"]["summary"] == "Receiver unavailable"
    assert data["health"]["lighting"]["label"] == "No lights added"


def test_overview_reports_expired_guest_without_refreshing_token(library_client):
    web, path = library_client
    config = json.loads(path.read_text())
    deadline = time.time() - 1
    config["spotify_profiles"]["profiles"]["test-account"].update({"kind": "guest", "expires_at": deadline})
    path.write_text(json.dumps(config))
    data = web.get("/api/owner/overview").get_json()
    assert data["account"]["connected"] is False
    assert data["account"]["profile_count"] == 0
    assert "test-refresh-token" not in path.read_text()


def test_library_album_detail_does_not_change_playback(library_client, monkeypatch):
    web, _ = library_client
    monkeypatch.setattr(server, "play_uri_local", lambda *_args, **_kwargs: pytest.fail("browsing must not play"))
    data = web.get("/api/library/item", query_string={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}).get_json()
    assert data["item"] == ALBUM
    assert data["tracks"] == [TRACK]
    assert data["kind"] == "album"
    assert data["tracks_status"] == "ready"
    assert data["message"] is None
    assert data["profile_epoch"] == "library-epoch"
    assert data["profile_state"] == "linked"


@pytest.mark.parametrize("epoch", [None, "old-profile-epoch"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_library_rejects_missing_or_stale_profile_epoch_before_metadata(library_client, monkeypatch, epoch, method):
    web, _ = library_client
    monkeypatch.setattr(server, "lookup_album_tracks", lambda *_args: pytest.fail("stale profile must not query metadata"))
    monkeypatch.setattr(server, "play_uri_local", lambda *_args, **_kwargs: pytest.fail("stale profile must not play"))
    params = {"uri": ALBUM["uri"]}
    if epoch is not None:
        params["profile_epoch"] = epoch
    response = web.get("/api/library/item", query_string=params) if method == "get" else web.post("/api/library/play", json=params)
    assert response.status_code == 409
    assert response.get_json()["code"] == "profile_changed"
    assert response.get_json()["profile_epoch"] == "library-epoch"


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("transition", ["handoff", "reauthorization"])
def test_library_rechecks_receiver_and_reauthorization_after_metadata(library_client, monkeypatch, method, transition):
    web, path = library_client

    def change_context(_album):
        if transition == "handoff":
            server._observe_receiver_identity("another-listener")
        else:
            config = json.loads(path.read_text())
            profile = config["spotify_profiles"]["profiles"]["test-account"]
            profile["authorized_at"] = time.time() - 366 * 86400
            profile["reauthorize_at"] = time.time() - 1
            path.write_text(json.dumps(config))
        return [TRACK]

    monkeypatch.setattr(server, "lookup_album_tracks", change_context)
    monkeypatch.setattr(server, "play_uri_local", lambda *_args, **_kwargs: pytest.fail("changed profile must not play"))
    params = {"uri": ALBUM["uri"], "profile_epoch": "library-epoch"}
    response = web.get("/api/library/item", query_string=params) if method == "get" else web.post("/api/library/play", json={**params, "track_uri": TRACK["uri"]})
    assert response.status_code == 409
    assert response.get_json()["code"] == "profile_changed"
    assert "First track" not in response.get_data(as_text=True)


def test_other_household_profile_cache_invalidation_does_not_disrupt_selection(library_client, monkeypatch):
    web, _ = library_client

    def other_profile_changed(_album):
        server._clear_user_caches("another-account")
        return [TRACK]

    monkeypatch.setattr(server, "lookup_album_tracks", other_profile_changed)
    response = web.get("/api/library/item", query_string={"uri": ALBUM["uri"], "profile_epoch": "library-epoch"})
    assert response.status_code == 200
    assert response.get_json()["tracks"] == [TRACK]


def test_owner_pairing_readiness_preserves_live_https_and_receiver_requirements(library_client):
    web, path = library_client
    assert web.get("/api/owner/overview").get_json()["oauth"]["pairing_ready"] is False
    config = json.loads(path.read_text())
    config["public_base_url"] = "https://display.example"
    path.write_text(json.dumps(config))
    assert web.get("/api/owner/overview").get_json()["oauth"]["pairing_ready"] is True
    server._observe_receiver_identity(None, active=False)
    overview = web.get("/api/owner/overview").get_json()
    assert overview["account"]["connected"] is True  # Live idle household shelf preference.
    assert overview["oauth"]["pairing_ready"] is False
    assert "Play on Pi Display" in overview["oauth"]["reason"]


def test_library_detail_only_returns_presentation_fields(library_client):
    web, _ = library_client
    server._crate_cache["payload"]["sections"][0]["items"] = [{**ALBUM, "internal_account": "private-account-data", "refresh_token": "internal-refresh-token"}]
    response = web.get("/api/library/item", query_string={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]})
    assert response.get_json()["item"] == ALBUM
    assert response.headers["Cache-Control"] == "no-store"
    assert "private-account-data" not in response.get_data(as_text=True)
    assert "internal-refresh-token" not in response.get_data(as_text=True)


@pytest.mark.parametrize("uri", ["spotify:album:unlisted", "spotify:track:track-one", "https://example.com", "spotify:album:../../me", ""])
def test_library_rejects_unlisted_or_invalid_collections(library_client, monkeypatch, uri):
    web, _ = library_client
    monkeypatch.setattr(server, "lookup_album_tracks", lambda *_args: pytest.fail("must not proxy unlisted albums"))
    monkeypatch.setattr(server, "play_uri_local", lambda *_args, **_kwargs: pytest.fail("must not play unlisted collection"))
    assert web.get("/api/library/item", query_string={"profile_epoch": "library-epoch", "uri": uri}).status_code == 400
    assert web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": uri}).status_code == 400


def test_playlist_detail_explains_whole_playlist_playback(library_client, monkeypatch):
    web, _ = library_client
    monkeypatch.setattr(server, "lookup_album_tracks", lambda *_args: pytest.fail("playlist is not an album"))
    response = web.get("/api/library/item", query_string={"profile_epoch": "library-epoch", "uri": PLAYLIST["uri"]})
    assert response.get_json()["tracks_status"] == "playlist"
    assert response.get_json()["tracks"] == []
    assert response.get_json()["message"]


def test_album_with_unavailable_tracks_can_still_play_whole_record(library_client, monkeypatch):
    web, _ = library_client
    monkeypatch.setattr(server, "lookup_album_tracks", lambda *_args: [])
    calls = []
    monkeypatch.setattr(server, "play_uri_local", lambda uri, **kwargs: (calls.append((uri, kwargs)) or True, "ok"))
    data = web.get("/api/library/item", query_string={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}).get_json()
    assert data["tracks_status"] == "unavailable"
    assert data["message"]
    assert web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}).status_code == 200
    assert web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"], "track_uri": TRACK["uri"]}).status_code == 503
    assert calls == [(ALBUM["uri"], {})]


def test_album_track_playback_keeps_album_context_and_validates_membership(library_client, monkeypatch):
    web, _ = library_client
    calls = []
    monkeypatch.setattr(server, "play_uri_local", lambda uri, **kwargs: (calls.append((uri, kwargs)) or True, "ok"))
    assert web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"], "track_uri": "spotify:track:unlisted"}).status_code == 400
    assert web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": PLAYLIST["uri"], "track_uri": TRACK["uri"]}).status_code == 400
    assert web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"], "track_uri": TRACK["uri"]}).status_code == 200
    assert calls == [(ALBUM["uri"], {"skip_to_uri": TRACK["uri"]})]


@pytest.mark.parametrize("payload", [None, [], "wrong", {"uri": None}, {"uri": ALBUM["uri"], "track_uri": None}])
def test_library_play_rejects_wrong_shapes(library_client, payload):
    web, _ = library_client
    assert web.post("/api/library/play", json=payload).status_code == 400


@pytest.mark.parametrize("message,status", [("Local player API unavailable", 503), ("Local player is not ready yet", 503), ("Local player API error: 500", 502)])
def test_library_returns_actionable_receiver_errors(library_client, monkeypatch, message, status):
    web, _ = library_client
    monkeypatch.setattr(server, "play_uri_local", lambda *_args, **_kwargs: (False, message))
    response = web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]})
    assert response.status_code == status
    assert response.get_json()["error"] == message


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("tracks", [[TRACK], []])
def test_account_change_during_track_lookup_invalidates_detail_and_play(library_client, monkeypatch, method, tracks):
    web, _ = library_client

    def switched_account(_album):
        server._clear_user_caches()
        return tracks

    monkeypatch.setattr(server, "lookup_album_tracks", switched_account)
    monkeypatch.setattr(server, "play_uri_local", lambda *_args, **_kwargs: pytest.fail("must not play from replaced account"))
    response = web.get("/api/library/item", query_string={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}) if method == "get" else web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"], "track_uri": TRACK["uri"]})
    assert response.status_code == 409
    assert "First track" not in response.get_data(as_text=True)


@pytest.mark.parametrize("method", ["get", "post"])
def test_guest_expiry_during_track_lookup_prevents_detail_and_play(library_client, monkeypatch, method):
    web, path = library_client
    config = json.loads(path.read_text())
    config["spotify_profiles"]["profiles"]["test-account"].update({"kind": "guest", "expires_at": time.time() + 60})
    path.write_text(json.dumps(config))

    def expires_during_lookup(_album):
        config["spotify_profiles"]["profiles"]["test-account"]["expires_at"] = time.time() - 1
        path.write_text(json.dumps(config))
        return [TRACK]

    monkeypatch.setattr(server, "lookup_album_tracks", expires_during_lookup)
    monkeypatch.setattr(server, "play_uri_local", lambda *_args, **_kwargs: pytest.fail("must not play after guest expiry"))
    response = web.get("/api/library/item", query_string={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}) if method == "get" else web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"], "track_uri": TRACK["uri"]})
    assert response.status_code == 409
    assert "test-account" not in json.loads(path.read_text())["spotify_profiles"]["profiles"]


def test_expired_guest_cannot_use_fresh_cached_private_crate(library_client, monkeypatch):
    web, path = library_client
    config = json.loads(path.read_text())
    config["spotify_profiles"]["profiles"]["test-account"].update({"kind": "guest", "expires_at": time.time() - 1})
    path.write_text(json.dumps(config))
    monkeypatch.setattr(server, "lookup_album_tracks", lambda *_args: pytest.fail("expired private metadata must not be read"))
    assert web.get("/api/library/item", query_string={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}).status_code == 409
    assert "test-account" not in json.loads(path.read_text())["spotify_profiles"]["profiles"]
    assert web.get("/api/crate").get_json()["sections"] == []


def test_idle_play_uses_fresh_authorized_crate_before_launcher_fetch(library_client, monkeypatch):
    web, _ = library_client
    monkeypatch.setattr(server, "idle_launcher_payload", lambda **_kwargs: pytest.fail("must not wait for Spotify launcher fetch"))
    monkeypatch.setattr(server, "crate_payload", lambda: pytest.fail("fresh cached card already authorized"))
    monkeypatch.setattr(server, "play_uri_local", lambda uri: (uri == ALBUM["uri"], "ok"))
    assert web.post("/api/idle/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}).status_code == 200


def test_explorer_collection_play_uses_fresh_crate_without_track_or_playlist_fetch(library_client, monkeypatch):
    web, _ = library_client
    monkeypatch.setattr(server, "idle_launcher_payload", lambda **_kwargs: pytest.fail("must not fetch Spotify playlists"))
    monkeypatch.setattr(server, "lookup_album_tracks", lambda _album: pytest.fail("whole album playback needs no track lookup"))
    monkeypatch.setattr(server, "play_uri_local", lambda uri: (uri == ALBUM["uri"], "ok"))
    assert web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}).status_code == 200


def test_disconnect_invalidates_selected_record_even_when_public_metadata_is_cached(library_client, monkeypatch):
    web, _ = library_client
    assert web.get("/api/library/item", query_string={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}).status_code == 200
    assert web.post("/api/auth/disconnect").status_code == 204
    monkeypatch.setattr(server, "play_uri_local", lambda *_args, **_kwargs: pytest.fail("a disconnected selection must not play"))
    assert web.post("/api/library/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"], "track_uri": TRACK["uri"]}).status_code == 409


def test_remote_public_launcher_cannot_use_private_cached_crate(library_client, monkeypatch):
    web, _ = library_client
    monkeypatch.setattr(server, "idle_launcher_payload", lambda **_kwargs: {"playlists": []})
    monkeypatch.setattr(server, "play_uri_local", lambda *_args, **_kwargs: pytest.fail("private cached card must not play"))
    assert web.post("/api/idle/play", json={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}, environ_base=REMOTE).status_code == 400


def test_library_track_response_is_bounded_and_sanitized(library_client, monkeypatch):
    web, _ = library_client
    tracks = [None, {**TRACK, "duration_ms": "bad", "name": [], "number": True, "disc": float("inf")}] + [TRACK] * 600
    monkeypatch.setattr(server, "lookup_album_tracks", lambda _album: tracks)
    data = web.get("/api/library/item", query_string={"profile_epoch": "library-epoch", "uri": ALBUM["uri"]}).get_json()
    assert len(data["tracks"]) <= server.MAX_LIBRARY_TRACKS
    assert data["tracks"][0]["name"] == "Untitled track"
    assert data["tracks"][0]["duration_ms"] == 0
    assert data["tracks"][0]["number"] == 1
    assert data["tracks"][0]["disc"] == 1


def test_library_queue_appends_album_tracks_in_order_without_starting_playback(library_client, monkeypatch):
    web, _ = library_client
    tracks = [dict(TRACK, uri=f'spotify:track:ordered-{i}', number=i) for i in range(1, 4)]
    monkeypatch.setattr(server, 'lookup_album_tracks', lambda _: tracks)
    monkeypatch.setattr(server, 'play_uri_local', lambda *_: pytest.fail('queue must not start playback'))
    queued = []
    monkeypatch.setattr(server, '_queue_uri_local', lambda uri: (queued.append(uri) is None, 'ok'))
    response = web.post('/api/library/queue', json={'uri': ALBUM['uri'], 'profile_epoch': 'library-epoch'})
    assert response.status_code == 200
    assert response.get_json()['status'] == 'ok'
    assert response.get_json()['queued'] == response.get_json()['of'] == 3
    assert queued == [track['uri'] for track in tracks]
    assert not server._library_queue_lock.locked()


@pytest.mark.parametrize('payload,status', [
    ({'uri': ALBUM['uri']}, 409),
    ({'uri': ALBUM['uri'], 'profile_epoch': 'stale'}, 409),
    ({'uri': PLAYLIST['uri'], 'profile_epoch': 'library-epoch'}, 400),
    ({'uri': 'spotify:album:not-in-crate', 'profile_epoch': 'library-epoch'}, 400),
    ([], 400),
])
def test_library_queue_rejects_invalid_or_stale_selection(library_client, monkeypatch, payload, status):
    web, _ = library_client
    monkeypatch.setattr(server, '_queue_uri_local', lambda _: pytest.fail('must not queue'))
    monkeypatch.setattr(server, 'lookup_album_tracks', lambda _: pytest.fail('must not fetch tracks'))
    response = web.post('/api/library/queue', json=payload)
    assert response.status_code == status


def test_library_queue_requires_owner_and_same_origin(library_client, monkeypatch):
    web, _ = library_client
    monkeypatch.setattr(server, '_queue_uri_local', lambda _: pytest.fail('must not queue'))
    payload = {'uri': ALBUM['uri'], 'profile_epoch': 'library-epoch'}
    assert web.post('/api/library/queue', json=payload, environ_base=REMOTE).status_code == 401
    assert web.post('/api/library/queue', json=payload, headers={'Origin': 'https://other.example'}).status_code == 403


def test_library_queue_reports_partial_failure_without_retry(library_client, monkeypatch):
    web, _ = library_client
    monkeypatch.setattr(server, 'lookup_album_tracks', lambda _: [dict(TRACK, uri=f'spotify:track:{i}') for i in range(3)])
    calls = []
    def enqueue(uri):
        calls.append(uri)
        return (len(calls) == 1, 'Receiver unavailable')
    monkeypatch.setattr(server, '_queue_uri_local', enqueue)
    response = web.post('/api/library/queue', json={'uri': ALBUM['uri'], 'profile_epoch': 'library-epoch'})
    data = response.get_json()
    assert response.status_code == 200 and data['status'] == 'partial'
    assert data['queued'] == 1 and data['of'] == 3
    assert len(calls) == 2 and not server._library_queue_lock.locked()


def test_library_queue_does_not_count_no_active_session_as_success(library_client, monkeypatch):
    web, _ = library_client
    class NoSession:
        status_code = 204
    monkeypatch.setattr(server._http, 'post', lambda *_args, **_kwargs: NoSession())
    response = web.post('/api/library/queue', json={'uri': ALBUM['uri'], 'profile_epoch': 'library-epoch'})
    assert response.status_code == 503
    assert response.get_json()['queued'] == 0
    assert 'Pi Display' in response.get_json()['error']


def test_library_queue_stops_on_receiver_handoff(library_client, monkeypatch):
    web, _ = library_client
    monkeypatch.setattr(server, 'lookup_album_tracks', lambda _: [dict(TRACK), dict(TRACK, uri='spotify:track:second')])
    calls = []
    def enqueue(uri):
        calls.append(uri)
        server._receiver_identity['epoch'] = 'new-listener'
        return True, 'ok'
    monkeypatch.setattr(server, '_queue_uri_local', enqueue)
    response = web.post('/api/library/queue', json={'uri': ALBUM['uri'], 'profile_epoch': 'library-epoch'})
    assert response.status_code == 409 and response.get_json()['code'] == 'profile_changed'
    assert len(calls) == 1 and not server._library_queue_lock.locked()


def test_library_queue_has_time_budget_and_rejects_overlapping_album(library_client, monkeypatch):
    web, _ = library_client
    payload = {'uri': ALBUM['uri'], 'profile_epoch': 'library-epoch'}
    monkeypatch.setattr(server, '_queue_uri_local', lambda _: pytest.fail('must not queue'))
    server._library_queue_lock.acquire()
    try:
        assert web.post('/api/library/queue', json=payload).status_code == 409
    finally:
        server._library_queue_lock.release()
    monkeypatch.setattr(server, 'LIBRARY_QUEUE_BUDGET_SECONDS', 0)
    response = web.post('/api/library/queue', json=payload)
    assert response.status_code == 503 and response.get_json()['queued'] == 0
    assert not server._library_queue_lock.locked()


@pytest.mark.parametrize('route', ['/api/library/play', '/api/library/queue', '/api/idle/play'])
def test_unavailable_vinyl_cannot_be_started_by_direct_request(library_client, monkeypatch, route):
    web, _ = library_client
    cache = server._crate_caches['profile:test-account']['payload']
    cache['sections'][0]['items'] = [dict(ALBUM, playable=False, availability_note='Unavailable in the UK')]
    monkeypatch.setattr(server, '_queue_uri_local', lambda _: pytest.fail('must not queue'))
    monkeypatch.setattr(server, 'play_uri_local', lambda _: pytest.fail('must not play'))
    assert web.post(route, json={'uri': ALBUM['uri'], 'profile_epoch': 'library-epoch'}).status_code == 409
