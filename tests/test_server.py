import json
import os
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

import server


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture
def client(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "client_id": "client-id",
        "client_secret": "client-secret",
        "public_base_url": "http://localhost",
        "security": {"owner_token": "owner-secret", "session_secret": "test-session"},
    }))
    monkeypatch.setattr(server, "CONFIG_FILE", str(config_path))
    monkeypatch.setattr(server, "RECENT_SPINS_FILE", str(tmp_path / "recent.json"))
    server.app.config.update(TESTING=True, SECRET_KEY="test-session", MAX_CONTENT_LENGTH=64 * 1024)
    server._rate_buckets.clear()
    server._pairing_tokens.clear()
    server._kiosk_pairing.update({
        "url": None,
        "digest": None,
        "expires_at": 0,
        "profile_epoch": None,
    })
    server._legacy_migration_attempts.clear()
    server._profile_prune_next = 0.0
    server._lyrics_cache.clear()
    server._lyrics_failure_times.clear()
    server._lyrics_inflight.clear()
    server._lyrics_breaker_until = 0
    server._track_cache.clear()
    server._album_cache.clear()
    server._album_tracks_cache.clear()
    server._artist_albums_cache.clear()
    server._uri_image_cache.clear()
    server._uri_image_failed.clear()
    server._enrich_last_attempt.clear()
    server._playlist_cache = {"loaded_at": 0, "items": []}
    server._recent_spins = {"loaded": False, "items": []}
    server._last_spin_album = None
    server._crate_cache.update({"built_at": 0, "payload": None})
    server._crate_caches.clear()
    server._crate_caches["generic"] = server._crate_cache
    server._crate_building = False
    server._crate_building_key = None
    server._account_generation = 0
    server._profile_generations.clear()
    server._event_clients = 0
    server._background_started = False
    server._background_components_started.clear()
    with server._wled_devices_lock:
        server._wled_devices.clear()
    with server._wled_scan_condition:
        server._wled_scan_state.update({
            "demand_until": 0.0,
            "last_started": float("-inf"),
            "running": False,
            "worker_started": False,
        })
    server._backlight_controller = None
    server._user_token = None
    server._user_token_expiry = 0
    server._user_token_grant_id = None
    server._user_tokens.clear()
    with server._receiver_identity_lock:
        server._receiver_identity.update({
            "alias": None,
            "epoch": "test-receiver-epoch",
            "active": False,
        })
    yield server.app.test_client(), config_path


def remote(address="192.168.68.40"):
    return {"REMOTE_ADDR": address}


def receiver_status(username="receiver-user"):
    return {
        "username": username,
        "paused": False,
        "buffering": False,
        "volume": 25,
        "volume_steps": 100,
        "track": {
            "uri": "spotify:track:active",
            "name": "Active",
            "artist_names": ["Artist"],
            "album_name": "Album",
            "duration": 180000,
            "position": 1000,
        },
    }


def spotify_get_for_identity(username="receiver-user", account_id="account-stable"):
    def get(url, **_kwargs):
        if url.endswith("/status"):
            return FakeResponse(payload=receiver_status(username))
        if url.endswith("/me"):
            return FakeResponse(payload={
                "id": username,
                "account_id": account_id,
                "display_name": "Listener",
            })
        raise AssertionError(f"unexpected GET {url}")
    return get


def store_profile(config_path, account_id="account-stable", alias="receiver-user", token="refresh"):
    config = json.loads(config_path.read_text())
    profiles = config.setdefault("spotify_profiles", {}).setdefault("profiles", {})
    profiles[account_id] = {
        "account_id": account_id,
        "display_name": account_id,
        "refresh_token": token,
        "kind": "owner",
        "connected_at": time.time(),
        "expires_at": None,
        "scopes": ["user-library-read", "user-top-read"],
        "receiver_aliases": [alias],
    }
    config_path.write_text(json.dumps(config))


def test_idle_playlist_loader_skips_wrong_json_shapes(client, tmp_path, monkeypatch):
    _web, _ = client
    source = tmp_path / "idle.json"
    monkeypatch.setattr(server, "IDLE_PLAYLISTS_FILE", str(source))

    source.write_text(json.dumps(["wrong-root"]))
    assert server.load_idle_playlists() == []

    server._playlist_cache = {"loaded_at": 0, "items": []}
    source.write_text(json.dumps({
        "playlists": [
            None,
            "wrong-entry",
            {"uri": 42},
            {
                "uri": "spotify:playlist:safe",
                "title": ["wrong-title"],
                "subtitle": None,
                "image": "/static/safe.svg",
                "accent": 123,
            },
        ],
    }))
    assert server.load_idle_playlists() == [{
        "id": "house-3",
        "title": "Playlist",
        "subtitle": "House pick",
        "uri": "spotify:playlist:safe",
        "image": "/static/safe.svg",
        "accent": "#ffffff",
    }]


def test_recent_spin_loader_skips_wrong_json_shapes(client, tmp_path, monkeypatch):
    _web, _ = client
    source = tmp_path / "recent.json"
    monkeypatch.setattr(server, "RECENT_SPINS_FILE", str(source))

    source.write_text(json.dumps(["wrong-root"]))
    server._load_recent_spins()
    assert server._recent_spins == {"loaded": True, "items": []}

    server._recent_spins = {"loaded": False, "items": []}
    source.write_text(json.dumps({
        "items": [
            None,
            "wrong-entry",
            {"uri": 42},
            {"uri": "spotify:album:safe", "artist_ids": ["artist", None, 7]},
        ],
    }))
    server._load_recent_spins()
    assert server._recent_spins["items"] == [{
        "uri": "spotify:album:safe",
        "artist_ids": ["artist"],
    }]


def test_legacy_runtime_state_rejects_symlinks_and_oversized_files(client, tmp_path, monkeypatch):
    _web, _ = client
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"is_playing": True}))
    state_file = tmp_path / "spotify-state.json"
    state_file.symlink_to(target)
    monkeypatch.setattr(server, "STATE_FILE", str(state_file))
    monkeypatch.setattr(server, "LEGACY_STATE_FILE", str(state_file))

    assert server._read_legacy_state_file()[0] is None

    state_file.unlink()
    state_file.write_bytes(b"x" * (server.MAX_RUNTIME_STATE_BYTES + 1))
    assert server._read_legacy_state_file()[0] is None


def test_wled_runtime_status_rejects_symlinks(client, tmp_path, monkeypatch):
    _web, _ = client
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"schema_version": 1, "updated_unix": time.time()}))
    status_file = tmp_path / "wled-status.json"
    status_file.symlink_to(target)
    monkeypatch.setattr(server, "WLED_STATUS_FILE", str(status_file))

    assert server._read_wled_runtime_status() == {
        "ok": False,
        "reason": "status_file_invalid",
    }


def test_oauth_error_is_json_not_reflected_html(client):
    web, _ = client
    response = web.get("/callback?error=%3Cscript%3Ealert(1)%3C/script%3E")
    assert response.status_code == 400
    assert response.is_json
    assert b"<script>" not in response.data


@pytest.mark.parametrize("path", [
    "/api/auth/owner",
    "/api/control/seek",
    "/api/control/volume",
    "/api/backlight",
    "/api/idle/play",
    "/api/album/play-track",
    "/api/wled/devices",
])
def test_mutation_routes_reject_wrong_shaped_json_roots(client, path):
    web, _ = client
    response = web.post(path, json=["not", "an", "object"])
    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object required"


@pytest.mark.parametrize("path,body", [
    ("/api/control/seek", {"position_ms": True}),
    ("/api/control/volume", {"percent": False}),
    ("/api/idle/play", {"uri": []}),
    ("/api/album/play-track", {"uri": 42}),
])
def test_mutation_routes_reject_wrong_scalar_types(client, path, body):
    web, _ = client
    assert web.post(path, json=body).status_code == 400


def test_oauth_uses_state_and_pkce_and_stores_expiring_guest(client, monkeypatch):
    web, config_path = client
    server._observe_receiver_identity("receiver-user")
    pairing = web.post("/api/auth/pairing").get_json()
    parsed_pairing = urlsplit(pairing["join_url"])
    assert web.get(parsed_pairing.path, environ_base=remote()).status_code == 302
    login = web.get("/login", environ_base=remote())
    assert login.status_code == 302
    query = parse_qs(urlsplit(login.location).query)
    assert query["state"][0]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) >= 43

    captured = {}

    def token_exchange(_url, **kwargs):
        captured.update(kwargs)
        return FakeResponse(payload={
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 60,
            "scope": "playlist-read-private user-library-read user-top-read",
        })

    monkeypatch.setattr(server.requests, "post", token_exchange)
    monkeypatch.setattr(server.requests, "get", spotify_get_for_identity())
    callback = web.get(
        f"/callback?code=abc&state={query['state'][0]}",
        environ_base=remote(),
    )
    assert callback.status_code == 302
    assert captured["data"]["code_verifier"]
    saved = json.loads(config_path.read_text())
    profile = saved["spotify_profiles"]["profiles"]["account-stable"]
    assert profile["refresh_token"] == "refresh"
    assert profile["kind"] == "guest"
    assert profile["expires_at"] > time.time()
    assert profile["receiver_aliases"] == ["receiver-user"]


def test_oauth_rejects_wrong_state_without_token_exchange(client, monkeypatch):
    web, _ = client
    web.get("/login")
    monkeypatch.setattr(server.requests, "post", lambda *_a, **_k: pytest.fail("must not exchange"))
    response = web.get("/callback?code=abc&state=wrong")
    assert response.status_code == 400


@pytest.mark.parametrize("payload", [
    ["wrong-shape"],
    {"access_token": [], "refresh_token": {}},
    {"access_token": "access", "refresh_token": 42},
])
def test_oauth_malformed_token_payload_degrades_to_502(client, monkeypatch, payload):
    web, _ = client
    login = web.get("/login")
    query = parse_qs(urlsplit(login.location).query)
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(payload=payload),
    )

    response = web.get(f"/callback?code=abc&state={query['state'][0]}")

    assert response.status_code == 502
    assert "token response" in response.get_json()["error"]


def test_oauth_invalid_expiry_uses_bounded_default(client, monkeypatch):
    web, _ = client
    login = web.get("/login")
    query = parse_qs(urlsplit(login.location).query)
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(payload={
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": "not-a-number",
        }),
    )
    monkeypatch.setattr(server.requests, "get", spotify_get_for_identity())
    before = time.time()

    response = web.get(f"/callback?code=abc&state={query['state'][0]}")

    assert response.status_code == 302
    assert before + 3500 < server._user_token_expiry < before + 3700


def test_pairing_link_is_one_use_and_only_allows_oauth_start(client):
    web, _ = client
    server._observe_receiver_identity("receiver-user")
    pairing = web.post("/api/auth/pairing")
    assert pairing.status_code == 200
    path = urlsplit(pairing.get_json()["join_url"]).path + "?" + urlsplit(pairing.get_json()["join_url"]).query

    consumed = web.get(path, environ_base=remote())
    assert consumed.status_code == 302
    assert consumed.location == "/join"
    login = web.get("/login?playlist=1", environ_base=remote())
    assert login.status_code == 302
    assert web.get("/api/wled/status", environ_base=remote()).status_code == 401
    assert web.get(path, environ_base=remote()).status_code == 400


def test_pairing_forces_expiring_guest_even_without_playlist_query(client, monkeypatch):
    web, config_path = client
    server._observe_receiver_identity("receiver-user")
    pairing = web.post("/api/auth/pairing").get_json()
    parsed = urlsplit(pairing["join_url"])
    path = parsed.path + "?" + parsed.query
    assert web.get(path, environ_base=remote()).status_code == 302

    login = web.get("/login", environ_base=remote())
    query = parse_qs(urlsplit(login.location).query)
    assert "user-library-read" in query["scope"][0]
    assert "playlist-read-private" in query["scope"][0]
    with web.session_transaction() as oauth_session:
        assert oauth_session["spotify_oauth"]["guest"] is True

    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *_a, **_k: FakeResponse(payload={
            "access_token": "guest-access",
            "refresh_token": "guest-refresh",
            "expires_in": 3600,
        }),
    )
    monkeypatch.setattr(server.requests, "get", spotify_get_for_identity())
    callback = web.get(
        f"/callback?code=abc&state={query['state'][0]}",
        environ_base=remote(),
    )
    assert callback.status_code == 302
    grant = json.loads(config_path.read_text())["spotify_profiles"]["profiles"]["account-stable"]
    assert grant["kind"] == "guest"
    assert grant["expires_at"] > time.time()


def test_owner_oauth_requests_private_library_scopes(client):
    web, _ = client
    login = web.get("/login")
    query = parse_qs(urlsplit(login.location).query)
    scope = query["scope"][0].split()
    assert "playlist-read-private" in scope
    assert "user-library-read" in scope
    with web.session_transaction() as oauth_session:
        assert oauth_session["spotify_oauth"]["guest"] is False


def test_oauth_requires_one_configured_public_origin(client):
    web, config_path = client
    config = json.loads(config_path.read_text())
    config.pop("public_base_url")
    config_path.write_text(json.dumps(config))
    missing = web.post("/api/auth/pairing")
    assert missing.status_code == 503
    assert "PUBLIC_BASE_URL" in missing.get_json()["error"]

    config["public_base_url"] = "https://display.example"
    config["redirect_uri"] = "https://other.example/callback"
    config_path.write_text(json.dumps(config))
    mismatch = web.get(
        "/login",
        headers={"X-Owner-Token": "owner-secret", "Host": "display.example"},
        environ_base=remote(),
    )
    assert mismatch.status_code == 503
    assert "share the public_base_url origin" in mismatch.get_json()["error"]


def test_oauth_accepts_explicit_https_reverse_proxy_origin(client, monkeypatch):
    web, config_path = client
    config = json.loads(config_path.read_text())
    config["public_base_url"] = "https://display.example"
    config.pop("redirect_uri", None)
    config_path.write_text(json.dumps(config))
    login = web.get(
        "/login",
        headers={"X-Owner-Token": "owner-secret", "Host": "display.example"},
        environ_base=remote(),
    )
    assert login.status_code == 302
    query = parse_qs(urlsplit(login.location).query)
    assert query["redirect_uri"] == ["https://display.example/callback"]

    monkeypatch.setattr(server, "control_playback", lambda _action: (True, "ok"))
    monkey_response = web.post(
        "/api/control/next",
        headers={"Origin": "https://display.example", "Host": "127.0.0.1:5000"},
        environ_base=remote(),
    )
    assert monkey_response.status_code == 200


def test_secure_cookie_uses_public_base_not_redirect_alone(client, monkeypatch):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config.pop("public_base_url", None)
    config["redirect_uri"] = "https://display.example/callback"
    config_path.write_text(json.dumps(config))
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    try:
        server._configure_cookie_security()
        assert server.app.config["SESSION_COOKIE_SECURE"] is False
        config["public_base_url"] = "https://display.example"
        config_path.write_text(json.dumps(config))
        server._configure_cookie_security()
        assert server.app.config["SESSION_COOKIE_SECURE"] is True
    finally:
        server.app.config["SESSION_COOKIE_SECURE"] = False


@pytest.mark.parametrize("redirect_uri", [
    "http://localhost/not-callback",
    "http://localhost/callback/",
    "http://localhost/callback?",
    "http://localhost/callback#",
    "http://localhost/callback?next=1",
    "http://localhost/callback#fragment",
])
def test_oauth_redirect_requires_exact_callback_path(client, redirect_uri):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config["redirect_uri"] = redirect_uri
    config_path.write_text(json.dumps(config))
    with pytest.raises(server.OAuthOriginError, match="exactly"):
        server.get_oauth_redirect_uri()


def test_oauth_redirect_accepts_plain_callback_without_query(client):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config["redirect_uri"] = "http://localhost/callback"
    config_path.write_text(json.dumps(config))
    assert server.get_oauth_redirect_uri() == "http://localhost/callback"


@pytest.mark.parametrize("public_base_url", [
    "http://localhost?",
    "http://localhost#",
    "http://localhost/?",
    "http://localhost/#",
])
def test_oauth_public_base_rejects_empty_delimiters(client, public_base_url):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config["public_base_url"] = public_base_url
    config_path.write_text(json.dumps(config))
    with pytest.raises(server.OAuthOriginError, match="bare"):
        server.get_oauth_public_base_url()


def test_ttl_cache_pop_cannot_return_expired_value(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    cache = server.BoundedTTLCache(2, 10)
    cache.set("nonce", True)
    now[0] = 111.0
    assert cache.pop("nonce", None) is None


def test_remote_sensitive_routes_require_owner_token(client):
    web, _ = client
    assert web.get("/api/crate", environ_base=remote()).status_code == 401
    authorized = web.get(
        "/api/wled/status",
        headers={"Authorization": "Bearer owner-secret"},
        environ_base=remote(),
    )
    assert authorized.status_code == 200


def test_owner_session_is_revoked_when_owner_token_rotates(client):
    web, config_path = client
    login = web.post(
        "/api/auth/owner",
        json={"token": "owner-secret"},
        environ_base=remote(),
    )
    assert login.status_code == 200
    assert web.get("/api/auth/status", environ_base=remote()).status_code == 200
    with web.session_transaction() as owner_session:
        serialised = json.dumps(dict(owner_session))
        assert "owner_token_binding" in owner_session
        assert "owner-secret" not in serialised
        assert owner_session.get("owner") is None

    config = json.loads(config_path.read_text())
    config["security"]["owner_token"] = "rotated-owner-secret"
    config_path.write_text(json.dumps(config))

    assert web.get("/api/auth/status", environ_base=remote()).status_code == 401


def test_loopback_owner_trust_rejects_non_loopback_host_get_and_post(client):
    web, _ = client
    rebound = {"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "attacker.example"}
    assert web.get("/api/crate", environ_overrides=rebound).status_code == 401
    assert web.post(
        "/api/wled/devices",
        json={"devices": []},
        environ_overrides=rebound,
    ).status_code == 401
    assert web.post(
        "/api/backlight",
        json={"percent": 20},
        environ_overrides=rebound,
    ).status_code == 403


def test_reverse_proxy_markers_disqualify_implicit_loopback_owner(client):
    web, _ = client
    proxied = {
        "REMOTE_ADDR": "127.0.0.1",
        "HTTP_HOST": "127.0.0.1:5000",
        "HTTP_X_FORWARDED_FOR": "192.168.68.40",
        "HTTP_X_FORWARDED_PROTO": "https",
    }
    assert web.get("/api/crate", environ_overrides=proxied).status_code == 401
    assert web.post(
        "/api/backlight",
        json={"percent": 20},
        environ_overrides=proxied,
    ).status_code == 403


def test_reverse_proxy_preserved_public_host_is_not_implicit_owner(client):
    web, _ = client
    proxied = {
        "REMOTE_ADDR": "127.0.0.1",
        "HTTP_HOST": "display.example",
    }
    assert web.get("/api/crate", environ_overrides=proxied).status_code == 401


def test_auth_api_responses_are_never_cached(client):
    web, _ = client
    for response in (
        web.get("/api/auth/status"),
        web.post("/api/auth/pairing"),
        web.post("/api/auth/owner", json={"token": "wrong"}),
    ):
        assert response.headers["Cache-Control"] == "no-store"


def test_idle_playlist_response_is_never_cached(client):
    web, _ = client
    response = web.get("/api/idle/playlists")
    assert response.headers["Cache-Control"] == "no-store"


def test_cross_site_mutation_is_rejected_before_control(client, monkeypatch):
    web, _ = client
    monkeypatch.setattr(server, "control_playback", lambda _action: pytest.fail("must not control"))
    response = web.post(
        "/api/control/next",
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403


def test_gesture_rate_limits_allow_final_volume_and_backlight_values(client, monkeypatch):
    web, _ = client

    def fake_request(url, **_kwargs):
        if url.endswith("/status"):
            return FakeResponse(payload={"volume_steps": 100})
        return FakeResponse(status_code=200)

    monkeypatch.setattr(server.requests, "get", fake_request)
    monkeypatch.setattr(server.requests, "post", fake_request)
    for percent in range(60):
        response = web.post("/api/control/volume", json={"percent": percent})
        assert response.status_code == 200
    assert response.get_json()["percent"] == 59

    class FakeBacklight:
        def set_percent(self, percent):
            return {"percent": int(percent)}

        def set_idle(self):
            return {"mode": "idle"}

        def set_active(self):
            return {"mode": "active"}

    monkeypatch.setattr(server, "_get_backlight_controller", lambda: FakeBacklight())
    for percent in range(60):
        response = web.post("/api/backlight", json={"percent": percent})
        assert response.status_code == 200
    assert response.get_json()["percent"] == 59
    assert server._mutation_rate_policy("/api/wled/devices") == (10, 60)


@pytest.mark.parametrize("device,error_fragment", [
    ({"host": "192.168.68.67", "pixel_count": "oops"}, "integer"),
    ({"host": "192.168.68.67", "pixel_count": 2049}, "between"),
    ({"host": "8.8.8.8", "pixel_count": 46}, "local network"),
])
def test_wled_validation_rejects_bad_devices(client, device, error_fragment):
    web, _ = client
    response = web.post("/api/wled/devices", json={"devices": [device]})
    assert response.status_code == 400
    assert error_fragment in response.get_json()["error"]


def test_wled_update_preserves_bounded_render_fields_atomically(client):
    web, config_path = client
    device = {
        "host": "wled.local",
        "name": "Record halo",
        "pixel_count": 256,
        "reverse": True,
        "phase_offset": -0.25,
        "brightness": 0.7,
        "gamma": 2.2,
    }
    response = web.post("/api/wled/devices", json={"devices": [device], "enabled": True})
    assert response.status_code == 204
    assert json.loads(config_path.read_text())["wled"]["devices"] == [device]
    assert os.stat(config_path).st_mode & 0o777 == 0o600
    assert web.get("/api/wled/status").get_json()["devices"] == [device]


def test_wled_discovery_is_owner_scoped_and_requests_scan(client, monkeypatch):
    web, _ = client
    requested = []
    monkeypatch.setattr(server, "_request_wled_scan", lambda: requested.append(True))
    server._wled_record_device("Record halo", "192.168.68.67", 80, 120)

    unauthorized = web.get("/api/wled/discovered", environ_base=remote())
    assert unauthorized.status_code == 401
    assert requested == []

    response = web.get("/api/wled/discovered")
    assert response.status_code == 200
    assert requested == [True]
    assert response.get_json()["devices"] == [{
        "ip": "192.168.68.67",
        "name": "Record halo",
        "pixel_count": 120,
        "port": 80,
    }]


def test_wled_scan_claim_requires_recent_demand_and_is_single_flight(client):
    _web, _ = client
    assert server._claim_wled_scan(now=100) is False

    server._request_wled_scan(now=100)
    assert server._claim_wled_scan(now=100) is True
    assert server._claim_wled_scan(now=101) is False

    server._finish_wled_scan()
    assert server._claim_wled_scan(now=105) is False
    # The short demand window expires before another interval is due, leaving
    # the worker dormant until the UI asks again.
    assert server._wled_scan_wait_timeout(now=111) is None
    assert server._claim_wled_scan(now=131) is False

    server._request_wled_scan(now=131)
    assert server._claim_wled_scan(now=131) is True
    server._finish_wled_scan()


def test_wled_discovery_cache_retains_then_evicts_by_ttl(client, monkeypatch):
    _web, _ = client
    now = [100.0]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    server._wled_record_device("Record halo", "192.168.68.67", 80, 120)
    now[0] += server.WLED_DEVICE_TTL_SECONDS - 1
    assert server._wled_active_devices()[0]["ip"] == "192.168.68.67"
    now[0] += 2
    assert server._wled_active_devices() == []


def test_wled_scan_batch_caps_total_probe_work(client, monkeypatch):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config["wled"] = {
        "devices": [{"host": "record-halo.local", "pixel_count": 120}],
    }
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(server, "get_local_ip", lambda: "192.168.68.10")
    monkeypatch.setattr(
        server,
        "_local_scan_network",
        lambda: server.ipaddress.ip_network("192.168.68.0/22"),
    )
    probed = []

    def probe(host):
        probed.append(host)
        return None

    class InlineExecutor:
        def __init__(self, max_workers):
            assert max_workers == server.WLED_PROBE_CONCURRENCY

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def map(self, function, targets):
            return [function(target) for target in targets]

    monkeypatch.setattr(server, "_probe_wled", probe)
    monkeypatch.setattr(server.concurrent.futures, "ThreadPoolExecutor", InlineExecutor)

    network, cursor = server._scan_wled_lan_batch(None, 0)
    assert network == server.ipaddress.ip_network("192.168.68.0/22")
    assert len(probed) <= server.WLED_SCAN_BATCH_SIZE
    assert probed[0] == "record-halo.local"
    assert cursor == server.WLED_SCAN_BATCH_SIZE - 1


def test_manual_wled_config_cannot_expand_scan_budget(client, monkeypatch):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config["wled"] = {
        "devices": [
            {"host": f"strip-{index}.local", "pixel_count": 30}
            for index in range(server.WLED_SCAN_BATCH_SIZE + 50)
        ],
    }
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(server, "get_local_ip", lambda: "192.168.68.10")
    monkeypatch.setattr(
        server,
        "_local_scan_network",
        lambda: server.ipaddress.ip_network("192.168.68.0/22"),
    )
    probed = []

    class InlineExecutor:
        def __init__(self, max_workers):
            assert max_workers == server.WLED_PROBE_CONCURRENCY

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def map(self, function, targets):
            return [function(target) for target in targets]

    monkeypatch.setattr(server, "_probe_wled", lambda host: probed.append(host))
    monkeypatch.setattr(server.concurrent.futures, "ThreadPoolExecutor", InlineExecutor)

    server._scan_wled_lan_batch(None, 0)

    assert len(server._wled_config_devices(server.load_config()["wled"])) == server.MAX_WLED_DEVICES
    assert len(probed) <= server.WLED_SCAN_BATCH_SIZE


def test_wled_scanner_start_is_idempotent_and_does_not_scan_eagerly(client, monkeypatch):
    _web, _ = client
    threads = []

    class DormantThread:
        def __init__(self, *, target, name, daemon):
            threads.append((target, name, daemon))

        def start(self):
            # A real thread will enter the condition wait. Not invoking target
            # here also proves startup itself does not perform a synchronous
            # LAN scan in the request path.
            return None

    monkeypatch.setattr(server.threading, "Thread", DormantThread)
    assert server._start_wled_lan_scanner() is True
    assert server._start_wled_lan_scanner() is False
    assert len(threads) == 1
    assert threads[0][1:] == ("wled-lan-scan", True)


def test_request_body_limit(client):
    web, _ = client
    response = web.post(
        "/api/wled/devices",
        data=json.dumps({"devices": [], "padding": "x" * (70 * 1024)}),
        content_type="application/json",
    )
    assert response.status_code == 413


def test_malformed_config_is_visible_and_never_overwritten(client, monkeypatch):
    web, config_path = client
    original = b'{"client_id": "still-here", BROKEN'
    config_path.write_bytes(original)
    assert server.config_status() == {
        "ok": False,
        "state": "malformed",
        "writable": False,
    }

    write = web.post("/api/wled/devices", json={"devices": []})
    assert write.status_code == 503
    assert write.get_json()["config"]["state"] == "malformed"
    assert config_path.read_bytes() == original

    old_secret = server.app.secret_key
    try:
        server._configure_session_secret()
        assert config_path.read_bytes() == original
        assert server.app.secret_key != old_secret
    finally:
        server.app.secret_key = old_secret

    monkeypatch.setattr(server, "read_go_librespot_state", lambda: (True, None))
    monkeypatch.setattr(server, "_read_legacy_state_file", lambda: (None, "/run/state", "missing"))
    health = web.get("/api/health")
    assert health.status_code == 503
    assert health.get_json()["config"] == {
        "ok": False,
        "state": "malformed",
        "writable": False,
    }
    diagnostics = web.get("/api/diagnostics")
    assert diagnostics.status_code == 200
    assert diagnostics.get_json()["config"]["state"] == "malformed"
    assert config_path.read_bytes() == original


def test_missing_config_is_distinct_and_can_be_initialized(client):
    _web, config_path = client
    config_path.unlink()
    assert server.config_status()["state"] == "missing"
    server.update_config(lambda config: config.__setitem__("created", True))
    assert json.loads(config_path.read_text()) == {"created": True}


def test_wrong_typed_config_sections_are_normalized_before_consumers(client, monkeypatch):
    web, config_path = client
    config_path.write_text(json.dumps({
        "client_id": ["wrong"],
        "client_secret": {"wrong": True},
        "refresh_token": ["wrong"],
        "public_base_url": "http://localhost",
        "legacy_web_api_device_id": ["wrong"],
        "allow_web_api_control_fallback": "true",
        "guest_session_hours": [12],
        "security": [],
        "spotify_session": ["wrong"],
        "wled": {"host": ["wrong"], "devices": {"wrong": True}, "enabled": "true"},
        "backlight": "wrong",
    }))
    normalized = server.load_config()
    assert all(isinstance(normalized[name], dict) for name in (
        "security", "spotify_session", "wled", "backlight"
    ))
    assert normalized["client_id"] == ""
    assert normalized["legacy_web_api_device_id"] == ""
    assert normalized["allow_web_api_control_fallback"] is False
    assert normalized["guest_session_hours"] == 12

    monkeypatch.setattr(server, "read_go_librespot_state", lambda: (True, None))
    assert web.get("/api/auth/status").status_code == 200
    assert web.get("/api/wled/status").status_code == 200
    assert web.get("/api/diagnostics").status_code == 200
    assert server.get_user_token() is None

    # A mutable route writes the normalized mapping rather than crashing or
    # preserving a poison section for the next process restart.
    assert web.post("/api/wled/devices", json={"devices": []}).status_code == 204
    persisted = json.loads(config_path.read_text())
    assert isinstance(persisted["spotify_session"], dict)
    assert isinstance(persisted["wled"], dict)


def test_invalid_backlight_config_is_safely_coerced_before_construction(client, monkeypatch):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config["backlight"] = {
        "enabled": "false",
        "initial_percent": 12.5,
        "idle_percent": "not-a-number",
        "safe_max_percent": float("nan"),
    }
    # json.dumps emits NaN for compatibility with the stdlib parser used here.
    config_path.write_text(json.dumps(config))
    captured = {}
    fake = object()

    def construct(value):
        captured.update(value["backlight"])
        return fake

    monkeypatch.setattr(server.BacklightController, "from_application_config", construct)
    assert server._get_backlight_controller() is fake
    assert captured["enabled"] is False
    assert captured["initial_percent"] == 12
    assert captured["idle_percent"] == 10
    assert captured["safe_max_percent"] == 80


def test_background_startup_retries_only_failed_component(client, monkeypatch):
    _web, _ = client
    calls = []

    class FlakyBacklight:
        attempts = 0

        def start(self):
            self.attempts += 1
            calls.append("backlight")
            if self.attempts == 1:
                raise RuntimeError("temporary failure")

    backlight = FlakyBacklight()
    monkeypatch.setattr(server, "_get_backlight_controller", lambda: backlight)
    monkeypatch.setattr(server, "_start_wled_lan_scanner", lambda: calls.append("wled"))
    monkeypatch.setattr(server, "_rebuild_crate_async", lambda: calls.append("crate"))
    monkeypatch.setattr(server, "_ensure_event_monitor", lambda: calls.append("events"))

    server._start_background_services()
    assert server._background_started is False
    assert calls == ["backlight", "wled", "crate", "events"]
    server._start_background_services()
    assert server._background_started is True
    assert calls == ["backlight", "wled", "crate", "events", "backlight"]


def test_explicit_album_is_rejected_until_current_album_is_known(client, monkeypatch):
    web, _ = client
    monkeypatch.setattr(server, "current_album_id", lambda: None)
    monkeypatch.setattr(server, "lookup_album_tracks", lambda _album: pytest.fail("must not proxy"))
    response = web.get("/api/album/tracks?album_id=arbitrary")
    assert response.status_code == 409


def test_web_api_fallback_is_disabled_by_default(client, monkeypatch):
    _web, _ = client
    monkeypatch.setattr(server, "control_playback_local", lambda _action: (False, "unavailable"))
    monkeypatch.setattr(server, "control_playback_web_api", lambda _action: pytest.fail("wrong-device fallback"))
    assert server.control_playback("next") == (False, "unavailable")


def test_stale_fallback_does_not_report_healthy(client, monkeypatch):
    web, _ = client
    monkeypatch.setattr(server, "read_go_librespot_state", lambda: (False, None))
    stale = {
        "timestamp": time.time() - 600,
        "event": "paused",
        "track_id": "track",
        "is_playing": False,
    }
    monkeypatch.setattr(server, "_read_legacy_state_file", lambda: (stale, "/run/state.json", None))
    response = web.get("/api/health")
    assert response.status_code == 503
    assert response.get_json()["raspotify_state"]["stale_reason"] == "paused_state_too_old"


def test_now_playing_distinguishes_receiver_outage_from_idle(client, monkeypatch):
    web, _ = client
    monkeypatch.setattr(server, "read_playback_state_with_availability", lambda: (None, False))
    outage = web.get("/api/now-playing")
    assert outage.status_code == 503
    monkeypatch.setattr(server, "read_playback_state_with_availability", lambda: (None, True))
    assert web.get("/api/now-playing").status_code == 204


@pytest.mark.parametrize("payload", [
    [],
    {},
    {"track": []},
    {"track": {"uri": "spotify:track:x", "artist_names": "Artist"}},
    {"track": {"uri": "spotify:track:x", "artist_names": [], "duration": "180000"}},
    {"track": {"uri": "spotify:track:x", "artist_names": []}, "paused": "false"},
])
def test_go_librespot_wrong_schema_degrades_to_unavailable(client, monkeypatch, payload):
    web, _ = client
    monkeypatch.setattr(server.requests, "get", lambda *_a, **_k: FakeResponse(payload=payload))
    monkeypatch.setattr(
        server,
        "_read_legacy_state_file",
        lambda: (None, "/run/spotify-display/spotify-state.json", "state_file_missing"),
    )
    assert server.read_go_librespot_state() == (False, None)
    response = web.get("/api/now-playing")
    assert response.status_code == 503


def test_go_librespot_valid_numeric_schema_is_normalized(client, monkeypatch):
    _web, _ = client
    payload = {
        "paused": False,
        "buffering": False,
        "volume": 25,
        "volume_steps": 50,
        "track": {
            "uri": "spotify:track:abc",
            "name": "Song",
            "artist_names": ["Artist"],
            "album_name": "Album",
            "duration": 180000.0,
            "position": 1234.0,
        },
    }
    monkeypatch.setattr(server.requests, "get", lambda *_a, **_k: FakeResponse(payload=payload))
    available, state = server.read_go_librespot_state()
    assert available is True
    assert state["progress_ms"] == 1234
    assert state["item"]["duration_ms"] == 180000
    assert state["device"]["volume_percent"] == 50


def test_volume_control_tolerates_wrong_receiver_status_shape(client, monkeypatch):
    web, _ = client
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(payload=["wrong-shape"]),
    )
    sent = []
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *_args, **kwargs: sent.append(kwargs["json"]) or FakeResponse(status_code=204),
    )

    response = web.post("/api/control/volume", json={"percent": 50})

    assert response.status_code == 200
    assert sent == [{"volume": 50}]


@pytest.mark.parametrize("payload", [
    [],
    {},
    {"items": "wrong-shape"},
    {"items": [None, "bad", {"uri": []}]},
])
def test_private_crate_fetchers_tolerate_wrong_upstream_shapes(client, monkeypatch, payload):
    web, _ = client
    monkeypatch.setattr(server, "get_user_token", lambda: "user-token")
    monkeypatch.setattr(server, "get_client_token", lambda: "client-token")
    monkeypatch.setattr(server, "load_idle_playlists", lambda: [])
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(payload=payload),
    )

    assert server.fetch_user_playlists() == []
    assert server.fetch_saved_albums() == []
    assert server.fetch_artist_albums("artist") == []
    assert web.get("/api/idle/playlists").status_code == 200


def test_album_track_pagination_never_follows_upstream_url(client, monkeypatch):
    _web, _ = client
    monkeypatch.setattr(server, "get_client_token", lambda: "client-token")
    requested = []

    def fetch(url, **kwargs):
        requested.append((url, kwargs["params"]))
        return FakeResponse(payload={
            "items": [{"uri": "spotify:track:safe", "name": "Safe"}],
            "next": "https://attacker.example/steal-token",
        })

    monkeypatch.setattr(server.requests, "get", fetch)

    tracks = server.lookup_album_tracks("album-id")

    assert [track["uri"] for track in tracks] == ["spotify:track:safe"]
    assert requested == [(
        f"{server.SPOTIFY_API_BASE}/albums/album-id/tracks",
        {"limit": 50, "offset": 0},
    )]


def test_lyrics_are_current_track_scoped_and_cached(client, monkeypatch):
    web, _ = client
    state = {
        "is_playing": True,
        "item": {
            "id": "track-1",
            "name": "Song",
            "artists": [{"name": "One"}, {"name": "Two"}],
            "album": {"name": "Record"},
        },
    }
    monkeypatch.setattr(server, "read_playback_state", lambda: state)
    calls = []
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse(payload={"syncedLyrics": "[00:01]Hi"}),
    )
    url = "/api/lyrics?track=Song&artist=One%2C%20Two&album=Record&duration=180"
    assert web.get(url).get_json()["cached"] is False
    assert web.get(url).get_json()["cached"] is True
    assert len(calls) == 1
    assert web.get("/api/lyrics?track=Other&artist=One").status_code == 409


def test_lyrics_circuit_breaker_stops_repeated_timeouts(client, monkeypatch):
    web, _ = client
    monkeypatch.setattr(server, "read_playback_state", lambda: {
        "item": {"id": "track-1", "name": "Song", "artists": [{"name": "Artist"}], "album": {}}
    })
    calls = []

    def timeout(*_args, **_kwargs):
        calls.append(True)
        raise server.requests.Timeout("slow")

    monkeypatch.setattr(server.requests, "get", timeout)
    url = "/api/lyrics?track=Song&artist=Artist&duration=180"
    for _ in range(3):
        assert web.get(url).get_json()["status"] == "upstream_error"
    assert web.get(url).get_json()["status"] == "temporarily_unavailable"
    assert len(calls) == 3


def test_lyrics_same_track_requests_are_singleflight(client, monkeypatch):
    _web, _ = client
    state = {
        "item": {"id": "track-1", "name": "Song", "artists": [{"name": "Artist"}], "album": {}}
    }
    monkeypatch.setattr(server, "read_playback_state", lambda: state)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fetch(*_args, **_kwargs):
        calls.append(True)
        started.set()
        assert release.wait(2)
        return FakeResponse(payload={"syncedLyrics": "[00:01]Hi"})

    monkeypatch.setattr(server.requests, "get", fetch)
    results = []
    leader = threading.Thread(target=lambda: results.append(
        server.app.test_client().get("/api/lyrics?track=Song&artist=Artist")
    ))
    leader.start()
    assert started.wait(1)
    duplicate = server.app.test_client().get("/api/lyrics?track=Song&artist=Artist")
    assert duplicate.status_code == 202
    assert duplicate.get_json()["status"] == "pending"
    release.set()
    leader.join(2)
    assert results[0].status_code == 200
    assert len(calls) == 1


def test_cold_crate_build_is_singleflight(client, monkeypatch):
    _web, _ = client
    entered = threading.Event()
    calls = []

    def build(*_args, **_kwargs):
        calls.append(True)
        entered.set()
        time.sleep(0.1)
        return {"sections": [{"id": "house", "items": []}]}

    monkeypatch.setattr(server, "_build_crate_payload", build)
    results = []
    first = threading.Thread(target=lambda: results.append(server.crate_payload()))
    second = threading.Thread(target=lambda: results.append(server.crate_payload()))
    first.start()
    assert entered.wait(1)
    second.start()
    first.join(2)
    second.join(2)
    assert len(calls) == 1
    assert len(results) == 2


def test_old_account_crate_build_cannot_repopulate_after_invalidation(client, monkeypatch):
    _web, _ = client
    started = threading.Event()
    release = threading.Event()

    def old_account_build(*_args, **_kwargs):
        started.set()
        assert release.wait(2)
        return {"sections": [{"id": "private-old-account", "items": [{"id": "secret"}]}]}

    monkeypatch.setattr(server, "_build_crate_payload", old_account_build)
    server._rebuild_crate_async()
    assert started.wait(1)
    generation = server._account_generation
    server._clear_user_caches()
    assert server._account_generation == generation + 1
    release.set()
    deadline = time.time() + 2
    while server._crate_building and time.time() < deadline:
        time.sleep(0.01)
    assert server._crate_building is False
    assert server._crate_cache["payload"] is None


def test_user_token_refresh_is_singleflight(client, monkeypatch):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config["spotify_profiles"] = {"profiles": {"account-stable": {
        "account_id": "account-stable",
        "display_name": "Listener",
        "refresh_token": "refresh",
        "kind": "owner",
        "connected_at": time.time(),
        "expires_at": None,
        "scopes": ["user-library-read"],
        "receiver_aliases": ["receiver-user"],
    }}}
    config_path.write_text(json.dumps(config))
    server._observe_receiver_identity("receiver-user")
    calls = []

    def refresh(*_args, **_kwargs):
        calls.append(True)
        time.sleep(0.05)
        return FakeResponse(payload={"access_token": "shared", "expires_in": 3600})

    monkeypatch.setattr(server.requests, "post", refresh)
    results = []
    threads = [threading.Thread(target=lambda: results.append(server.get_user_token())) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert results == ["shared"] * 5
    assert len(calls) == 1


def test_expired_guest_grant_is_removed_without_refresh(client, monkeypatch):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config.update({
        "refresh_token": "expired",
        "spotify_session": {"kind": "guest", "expires_at": time.time() - 1},
    })
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(server.requests, "post", lambda *_a, **_k: pytest.fail("must not refresh"))
    assert server.get_user_token() is None
    saved = json.loads(config_path.read_text())
    assert "refresh_token" not in saved
    assert "spotify_session" not in saved


def test_manual_refresh_token_removal_invalidates_cached_access(client, monkeypatch):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config["spotify_profiles"] = {"profiles": {"account-stable": {
        "account_id": "account-stable",
        "display_name": "Listener",
        "refresh_token": "refresh",
        "kind": "owner",
        "connected_at": time.time(),
        "expires_at": None,
        "scopes": ["user-library-read"],
        "receiver_aliases": ["receiver-user"],
    }}}
    config_path.write_text(json.dumps(config))
    server._observe_receiver_identity("receiver-user")
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *_a, **_k: FakeResponse(payload={"access_token": "access", "expires_in": 3600}),
    )
    assert server.get_user_token() == "access"
    config = json.loads(config_path.read_text())
    config["spotify_profiles"] = {"profiles": {}}
    config_path.write_text(json.dumps(config))
    assert server.get_user_token() is None


def test_sse_signal_supports_tracks_without_spotify_id():
    with server._receiver_identity_lock:
        server._receiver_identity.update({
            "alias": None,
            "epoch": "test-receiver-epoch",
            "active": False,
        })
    signal = server._playback_event_signal({
        "is_playing": True,
        "progress_ms": 12345,
        "item": {"id": None, "uri": "local:track:1", "name": "Local track"},
    })
    assert signal == {
        "active": True,
        "track_id": "local:track:1",
        "is_playing": True,
        "progress_bucket": 1,
        "receiver_available": True,
        "profile_state": "no_receiver",
        "profile_epoch": "test-receiver-epoch",
    }


def test_sse_client_cap_rejects_exhaustion_and_releases_slots(client, monkeypatch):
    web, _ = client
    monkeypatch.setattr(server, "MAX_SSE_CLIENTS", 2)
    monkeypatch.setattr(server, "_ensure_event_monitor", lambda: None)
    first = web.get("/api/events", buffered=False)
    second = web.get("/api/events", buffered=False)
    assert first.status_code == second.status_code == 200
    rejected = web.get("/api/events")
    assert rejected.status_code == 503
    assert rejected.headers["Retry-After"] == "10"
    second.close()
    replacement = web.get("/api/events", buffered=False)
    assert replacement.status_code == 200
    replacement.close()
    first.close()
    assert server._event_clients == 0


def test_receiver_username_selects_context_but_is_never_public(client, monkeypatch):
    web, _ = client
    monkeypatch.setattr(server, "queue_track_enrichment", lambda _track_id: None)
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(payload=receiver_status("PrivateAlias")),
    )

    response = web.get("/api/now-playing")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["profile_state"] == "unlinked"
    assert payload["profile_epoch"]
    assert "PrivateAlias" not in response.get_data(as_text=True)
    assert "PrivateAlias" not in json.dumps(server._playback_event_signal(payload))


def test_receiver_outage_rotates_epoch_and_returns_only_opaque_context(client, monkeypatch):
    web, _ = client
    old_epoch = server._observe_receiver_identity("PrivateAlias")["epoch"]

    def offline(*_args, **_kwargs):
        raise server.requests.RequestException("offline")

    monkeypatch.setattr(server.requests, "get", offline)
    monkeypatch.setattr(
        server,
        "_read_legacy_state_file",
        lambda: (None, "/run/state", "state_file_missing"),
    )

    response = web.get("/api/now-playing")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["profile_state"] == "no_receiver"
    assert payload["profile_epoch"] != old_epoch
    assert response.headers["X-Spotify-Profile-State"] == "no_receiver"
    assert response.headers["X-Spotify-Profile-Epoch"] == payload["profile_epoch"]
    assert "PrivateAlias" not in response.get_data(as_text=True)

    signal = server._playback_event_signal(None, receiver_available=False)
    assert signal["receiver_available"] is False
    assert signal["profile_state"] == "no_receiver"
    assert signal["profile_epoch"] == payload["profile_epoch"]
    assert "PrivateAlias" not in json.dumps(signal)


def test_pairing_cookie_contains_only_opaque_epoch_not_receiver_identity(client):
    web, _ = client
    server._observe_receiver_identity("PrivateAlias")
    pairing = web.post("/api/auth/pairing").get_json()
    path = urlsplit(pairing["join_url"]).path
    assert re.fullmatch(r"/pair/[0-9A-HJKMNP-TV-Z]{12}", path)

    assert web.get(path.lower(), environ_base=remote()).status_code == 302
    with web.session_transaction() as browser_session:
        assert browser_session["oauth_pairing"]["profile_epoch"]
        serialised = json.dumps(dict(browser_session))
        assert "PrivateAlias" not in serialised
        assert "receiver_alias" not in serialised
        assert "account_id" not in serialised
        assert "display_name" not in serialised

    assert web.get("/login", environ_base=remote()).status_code == 302
    with web.session_transaction() as browser_session:
        serialised = json.dumps(dict(browser_session))
        assert "PrivateAlias" not in serialised
        assert "receiver_alias" not in serialised
        assert "account_id" not in serialised
        assert "display_name" not in serialised


def test_oauth_handoff_during_provider_io_cannot_publish_old_profile(client, monkeypatch):
    web, config_path = client
    server._observe_receiver_identity("receiver-user")
    pairing = web.post("/api/auth/pairing").get_json()
    assert web.get(urlsplit(pairing["join_url"]).path, environ_base=remote()).status_code == 302
    login = web.get("/login", environ_base=remote())
    state = parse_qs(urlsplit(login.location).query)["state"][0]
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(payload={
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
        }),
    )
    status_calls = 0

    def provider_get(url, **_kwargs):
        nonlocal status_calls
        if url.endswith("/me"):
            return FakeResponse(payload={
                "id": "receiver-user",
                "account_id": "stable-account",
                "display_name": "Listener",
            })
        if url.endswith("/status"):
            status_calls += 1
            if status_calls == 2:
                server._observe_receiver_identity("other-user")
            return FakeResponse(payload=receiver_status("receiver-user"))
        raise AssertionError(url)

    monkeypatch.setattr(server.requests, "get", provider_get)
    callback = web.get(f"/callback?code=abc&state={state}", environ_base=remote())

    assert callback.status_code == 409
    assert callback.get_json()["code"] == "profile_changed"
    assert not json.loads(config_path.read_text()).get("spotify_profiles", {}).get("profiles")


def test_oauth_rechecks_epoch_after_waiting_for_token_lock(client, monkeypatch):
    web, config_path = client
    server._observe_receiver_identity("receiver-user")
    pairing = web.post("/api/auth/pairing").get_json()
    assert web.get(urlsplit(pairing["join_url"]).path, environ_base=remote()).status_code == 302
    login = web.get("/login", environ_base=remote())
    state = parse_qs(urlsplit(login.location).query)["state"][0]
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(payload={
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
        }),
    )
    before_token_lock = threading.Event()
    lock_held = threading.Event()

    def provider_get(url, **_kwargs):
        if url.endswith("/me"):
            return FakeResponse(payload={
                "id": "receiver-user",
                "account_id": "stable-account",
                "display_name": "Listener",
            })
        if url.endswith("/status"):
            return FakeResponse(payload=receiver_status("receiver-user"))
        raise AssertionError(url)

    monkeypatch.setattr(server.requests, "get", provider_get)
    original_binding_matches = server._receiver_binding_matches
    binding_checks = 0

    def binding_matches(binding, refresh=False):
        nonlocal binding_checks
        result = original_binding_matches(binding, refresh=refresh)
        binding_checks += 1
        if binding_checks == 2:
            before_token_lock.set()
        return result

    monkeypatch.setattr(server, "_receiver_binding_matches", binding_matches)

    def hold_token_lock_then_handoff():
        with server._user_token_lock:
            lock_held.set()
            assert before_token_lock.wait(2)
            server._observe_receiver_identity("other-user")

    holder = threading.Thread(target=hold_token_lock_then_handoff)
    holder.start()
    assert lock_held.wait(1)
    callback = web.get(f"/callback?code=abc&state={state}", environ_base=remote())
    holder.join(2)

    assert callback.status_code == 409
    assert callback.get_json()["code"] == "profile_changed"
    assert not json.loads(config_path.read_text()).get("spotify_profiles", {}).get("profiles")


def test_stale_profile_epoch_makes_no_user_token_or_api_call(client, monkeypatch):
    _web, config_path = client
    store_profile(config_path, account_id="account-a", alias="alias-a", token="refresh-a")
    old_epoch = server._observe_receiver_identity("alias-a")["epoch"]
    server._observe_receiver_identity("alias-b")
    calls = []
    monkeypatch.setattr(server.requests, "post", lambda *_a, **_k: calls.append("post"))
    monkeypatch.setattr(server.requests, "get", lambda *_a, **_k: calls.append("get"))

    assert server.fetch_user_playlists(
        account_id="account-a", profile_epoch=old_epoch
    ) == []
    assert calls == []


def test_deferred_a_build_is_discarded_after_a_to_b_to_a_handoff(client, monkeypatch):
    _web, config_path = client
    store_profile(config_path, account_id="account-a", alias="alias-a", token="refresh-a")
    store_profile(config_path, account_id="account-b", alias="alias-b", token="refresh-b")
    context_a = server._receiver_context()
    server._observe_receiver_identity("alias-a")
    context_a = server._receiver_context()
    started = threading.Event()
    release = threading.Event()

    def delayed_build(account_id=None, profile_epoch=None):
        assert account_id == "account-a"
        assert profile_epoch == context_a["profile_epoch"]
        started.set()
        assert release.wait(2)
        return {"sections": [{"id": "private-a", "items": [{"id": "secret-a"}]}]}

    monkeypatch.setattr(server, "_build_crate_payload", delayed_build)
    server._rebuild_crate_async(context_a)
    assert started.wait(1)
    server._observe_receiver_identity("alias-b")
    server._observe_receiver_identity("alias-a")
    release.set()
    deadline = time.time() + 2
    while server._crate_building and time.time() < deadline:
        time.sleep(0.01)

    cache = server._crate_caches.get("profile:account-a")
    assert cache is None or cache["payload"] is None


def test_hot_a_cache_is_not_returned_after_handoff_during_lookup(client, monkeypatch):
    _web, config_path = client
    store_profile(config_path, account_id="account-a", alias="alias-a")
    store_profile(config_path, account_id="account-b", alias="alias-b")
    server._observe_receiver_identity("alias-a")
    server._crate_caches["profile:account-a"] = {
        "built_at": time.time(),
        "payload": {"sections": [{"id": "private-a", "items": [{"id": "secret"}]}]},
    }
    original = server._crate_cache_for

    def handoff_after_cache_lookup(cache_key):
        cache = original(cache_key)
        server._observe_receiver_identity("alias-b")
        return cache

    monkeypatch.setattr(server, "_crate_cache_for", handoff_after_cache_lookup)

    payload = server.crate_payload()

    assert payload["profile_state"] == "linked"
    assert payload["sections"] == []
    assert payload["building"] is True
    assert "secret" not in json.dumps(payload)


def test_invalid_grant_removes_only_affected_profile(client, monkeypatch):
    _web, config_path = client
    store_profile(config_path, account_id="account-a", alias="alias-a", token="refresh-a")
    store_profile(config_path, account_id="account-b", alias="alias-b", token="refresh-b")
    epoch = server._observe_receiver_identity("alias-a")["epoch"]
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            status_code=400, payload={"error": "invalid_grant"}
        ),
    )

    assert server.get_user_token("account-a", profile_epoch=epoch) is None
    profiles = json.loads(config_path.read_text())["spotify_profiles"]["profiles"]
    assert "account-a" not in profiles
    assert profiles["account-b"]["refresh_token"] == "refresh-b"


def test_rotated_profile_token_is_saved_even_when_refresh_handoffs(client, monkeypatch):
    _web, config_path = client
    store_profile(config_path, account_id="account-a", alias="alias-a", token="refresh-old")
    epoch = server._observe_receiver_identity("alias-a")["epoch"]

    def refresh(_refresh_token, _config):
        server._observe_receiver_identity("alias-b")
        return ({
            "access_token": "stale-access",
            "refresh_token": "refresh-rotated",
            "expires_in": 3600,
            "scope": "user-library-read",
        }, False)

    monkeypatch.setattr(server, "_refresh_token_response", refresh)

    assert server.get_user_token("account-a", profile_epoch=epoch) is None
    saved = json.loads(config_path.read_text())
    profile = saved["spotify_profiles"]["profiles"]["account-a"]
    assert profile["refresh_token"] == "refresh-rotated"
    assert "account-a" not in server._user_tokens


def test_legacy_rotation_is_preserved_when_alias_does_not_match(client, monkeypatch):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config.update({
        "refresh_token": "legacy-old",
        "spotify_session": {"kind": "owner", "connected_at": 1, "expires_at": None},
    })
    config_path.write_text(json.dumps(config))
    server._observe_receiver_identity("current-user")
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(payload={
            "access_token": "access",
            "refresh_token": "legacy-rotated",
            "expires_in": 3600,
        }),
    )
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(payload={
            "id": "different-user",
            "account_id": "different-account",
            "display_name": "Different",
        }),
    )

    context = server._maybe_migrate_legacy_profile(server._receiver_context())

    saved = json.loads(config_path.read_text())
    assert context["profile_state"] == "unlinked"
    assert saved["refresh_token"] == "legacy-rotated"
    assert not saved.get("spotify_profiles", {}).get("profiles")


def test_exact_legacy_identity_migrates_atomically(client, monkeypatch):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config.update({
        "refresh_token": "legacy-old",
        "spotify_session": {"kind": "owner", "connected_at": 1, "expires_at": None},
    })
    config_path.write_text(json.dumps(config))
    server._observe_receiver_identity("legacy-user")
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(payload={
            "access_token": "access",
            "refresh_token": "legacy-rotated",
            "expires_in": 3600,
            "scope": "user-library-read",
        }),
    )
    monkeypatch.setattr(
        server.requests,
        "get",
        spotify_get_for_identity("legacy-user", "stable-legacy-account"),
    )

    context = server._maybe_migrate_legacy_profile(server._receiver_context())

    saved = json.loads(config_path.read_text())
    profile = saved["spotify_profiles"]["profiles"]["stable-legacy-account"]
    assert context["profile_state"] == "linked"
    assert profile["refresh_token"] == "legacy-rotated"
    assert profile["receiver_aliases"] == ["legacy-user"]
    assert "refresh_token" not in saved
    assert "spotify_session" not in saved


def test_unlinked_receiver_gets_generic_crate_without_user_api_calls(client, monkeypatch):
    _web, _ = client
    server._observe_receiver_identity("unlinked-user")
    monkeypatch.setattr(server, "load_idle_playlists", lambda: [{
        "id": "house", "title": "House", "uri": "spotify:playlist:house"
    }])
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("must not request a user token"),
    )
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("must not call a private user API"),
    )

    payload = server.crate_payload()

    assert payload["profile_state"] == "unlinked"
    assert [section["id"] for section in payload["sections"]] == ["house"]


def test_idle_playlist_handoff_discards_private_payload_and_pairing_url(client, monkeypatch):
    web, config_path = client
    store_profile(config_path, account_id="account-a", alias="alias-a")
    store_profile(config_path, account_id="account-b", alias="alias-b")
    server._observe_receiver_identity("alias-a")
    calls = 0

    def launcher(include_private=True, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            server._observe_receiver_identity("alias-b")
            return {"title": "Your playlists", "playlists": [{
                "id": "private-a", "title": "Secret", "uri": "spotify:playlist:secret"
            }]}
        return {"title": "House picks", "playlists": []}

    monkeypatch.setattr(server, "idle_launcher_payload", launcher)

    response = web.get("/api/idle/playlists")
    payload = response.get_json()
    assert payload["playlists"] == []
    if payload["join_url"] is not None:
        assert server._kiosk_pairing["profile_epoch"] == payload["profile_epoch"]
    assert "secret" not in json.dumps(payload)


def test_pairing_route_rejects_receiver_change_before_response(client, monkeypatch):
    web, _ = client
    server._observe_receiver_identity("alias-a")

    def change_receiver(**_kwargs):
        server._observe_receiver_identity("alias-b")
        return "http://localhost/pair/0123456789AB"

    monkeypatch.setattr(server, "_new_pairing_url", change_receiver)
    response = web.post("/api/auth/pairing")
    assert response.status_code == 409
    assert response.get_json()["code"] == "profile_changed"


def test_receiver_context_is_read_only_while_crate_lock_is_held(client, monkeypatch):
    _web, _ = client
    monkeypatch.setattr(
        server,
        "_prune_expired_profiles",
        lambda *_a, **_k: pytest.fail("receiver context must not prune"),
    )
    with server._crate_build_lock:
        context = server._receiver_context()
    assert context["profile_state"] == "no_receiver"


def test_top_tracks_are_deduplicated_to_rotation_albums(client, monkeypatch):
    _web, config_path = client
    store_profile(config_path, account_id="account-a", alias="alias-a")
    epoch = server._observe_receiver_identity("alias-a")["epoch"]
    monkeypatch.setattr(
        server,
        "get_user_token",
        lambda _account_id=None, profile_epoch=None: "access",
    )
    album = {
        "id": "album-1",
        "uri": "spotify:album:album-1",
        "name": "Rotation Album",
        "images": [{"url": "https://example.test/cover.jpg"}],
        "artists": [{"name": "Artist"}],
    }
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(payload={
            "items": [{"album": album}, {"album": dict(album)}]
        }),
    )

    items = server.fetch_top_albums(
        account_id="account-a", profile_epoch=epoch
    )

    assert len(items) == 1
    assert items[0]["uri"] == "spotify:album:album-1"


def test_oauth_scopes_are_library_only_unless_fallback_is_enabled(client):
    _web, _ = client
    default = server._oauth_scopes({})
    assert set(default) == {
        "user-read-private",
        "playlist-read-private",
        "playlist-read-collaborative",
        "user-library-read",
        "user-top-read",
    }
    assert not set(default).intersection(server.PLAYBACK_SCOPES)
    fallback = server._oauth_scopes({"allow_web_api_control_fallback": True})
    assert set(server.PLAYBACK_SCOPES).issubset(fallback)


def test_expired_guest_profiles_are_pruned_before_capacity_insert(client):
    _web, config_path = client
    config = json.loads(config_path.read_text())
    config["spotify_profiles"] = {"profiles": {}}
    for index in range(32):
        config["spotify_profiles"]["profiles"][f"expired-{index}"] = {
            "account_id": f"expired-{index}",
            "display_name": "Expired",
            "refresh_token": f"refresh-{index}",
            "kind": "guest",
            "connected_at": 1,
            "expires_at": time.time() - 1,
            "scopes": ["user-library-read"],
            "receiver_aliases": [f"old-alias-{index}"],
        }
    config_path.write_text(json.dumps(config))

    server._persist_profile_grant({
        "account_id": "new-account",
        "display_name": "New",
        "refresh_token": "new-refresh",
        "kind": "guest",
        "connected_at": time.time(),
        "expires_at": time.time() + 3600,
        "scopes": ["user-library-read"],
        "receiver_aliases": ["new-alias"],
    }, "new-alias")

    profiles = json.loads(config_path.read_text())["spotify_profiles"]["profiles"]
    assert list(profiles) == ["new-account"]


def test_wled_runtime_status_is_schema_checked_and_reports_liveness(client, monkeypatch, tmp_path):
    _web, _ = client
    status_file = tmp_path / "wled-status.json"
    status_file.write_text(json.dumps({
        "schema_version": 1,
        "updated_unix": time.time(),
        "enabled": True,
        "configured_devices": 1,
        "rendering": True,
        "spin_speed": 1.0,
        "udp_datagrams_queued": 123,
        "udp_local_send_errors": 2,
        "hosts_seen": ["192.168.68.67"],
        "playback": {
            "state": "active",
            "thread_alive": True,
            "consecutive_failures": 0,
            "last_success_age_seconds": 0.5,
        },
    }))
    monkeypatch.setattr(server, "WLED_STATUS_FILE", str(status_file))
    runtime = server._read_wled_runtime_status()
    assert runtime["ok"] is True
    assert runtime["playback"]["thread_alive"] is True
    assert runtime["udp_local_send_errors"] == 2


def test_wled_runtime_status_rejects_oversize_or_unknown_schema(client, monkeypatch, tmp_path):
    _web, _ = client
    status_file = tmp_path / "wled-status.json"
    status_file.write_bytes(b"x" * (64 * 1024 + 1))
    monkeypatch.setattr(server, "WLED_STATUS_FILE", str(status_file))
    assert server._read_wled_runtime_status()["reason"] == "invalid_size"
    status_file.write_text(json.dumps({"schema_version": 2, "updated_unix": time.time()}))
    assert server._read_wled_runtime_status()["reason"] == "unsupported_schema"
