"""Tests for the Flask Spotify proxy server."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

import server


# ── Test data ───────────────────────────────────────────────────────

TEST_CONFIG = {
    "client_id": "test_client_id",
    "client_secret": "test_client_secret",
    "redirect_uri": "http://localhost:5000/callback",
    "access_token": "test_access_token",
    "refresh_token": "test_refresh_token",
    "token_expiry": time.time() + 3600,
}

TEST_CONFIG_NO_TOKEN = {
    "client_id": "test_client_id",
    "client_secret": "test_client_secret",
    "redirect_uri": "http://localhost:5000/callback",
}


# ── Helpers ─────────────────────────────────────────────────────────

def _mock_response(status_code=200, json_body=None):
    """Build a fake ``requests.Response``."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    resp.text = json.dumps(json_body) if json_body else ""
    resp.content = b""
    return resp


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _mock_load_config():
    """Every test gets a mocked load_config returning valid credentials."""
    with patch.object(server, "load_config", return_value=dict(TEST_CONFIG)):
        yield


@pytest.fixture(autouse=True)
def _mock_save_config():
    """Prevent real writes to config.json."""
    with patch.object(server, "save_config"):
        yield


@pytest.fixture(autouse=True)
def _mock_settings_io():
    """Prevent real reads/writes to settings.json."""
    with patch.object(
        server, "load_settings", return_value=dict(server.DEFAULT_SETTINGS)
    ), patch.object(server, "save_settings"):
        yield


@pytest.fixture()
def client():
    """Flask test client."""
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


# ── UI routes ───────────────────────────────────────────────────────

def test_index_returns_html(client):
    """GET / returns 200 with HTML content."""
    resp = client.get("/")
    assert resp.status_code == 200


# ── Login ───────────────────────────────────────────────────────────

def test_login_redirects_to_spotify(client):
    """GET /login returns 302 redirect to accounts.spotify.com."""
    resp = client.get("/login")
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "accounts.spotify.com" in location
    assert "test_client_id" in location


# ── OAuth callback ──────────────────────────────────────────────────

def test_callback_exchanges_code(client):
    """GET /callback?code=testcode exchanges the code and persists tokens."""
    token_data = {
        "access_token": "new_access",
        "refresh_token": "new_refresh",
        "expires_in": 3600,
    }
    with patch("server.requests.post", return_value=_mock_response(200, token_data)), \
         patch.object(server, "save_config") as mock_save:
        resp = client.get("/callback?code=testcode")

    assert resp.status_code == 200
    assert b"Authenticated" in resp.data
    mock_save.assert_called_once()
    saved = mock_save.call_args[0][0]
    assert saved["access_token"] == "new_access"
    assert saved["refresh_token"] == "new_refresh"


def test_callback_error(client):
    """GET /callback?error=access_denied returns 400."""
    resp = client.get("/callback?error=access_denied")
    assert resp.status_code == 400
    assert b"access_denied" in resp.data


# ── Now Playing ─────────────────────────────────────────────────────

def test_now_playing_returns_data(client):
    """GET /api/now-playing proxies to Spotify and returns track data."""
    body = {"is_playing": True, "item": {"name": "Test Song"}}
    with patch("server.requests.request", return_value=_mock_response(200, body)):
        resp = client.get("/api/now-playing")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["is_playing"] is True
    assert data["item"]["name"] == "Test Song"


def test_now_playing_unauthenticated(client):
    """Returns 401 when no token is available."""
    with patch.object(server, "load_config", return_value=dict(TEST_CONFIG_NO_TOKEN)):
        resp = client.get("/api/now-playing")

    assert resp.status_code == 401
    data = resp.get_json()
    assert "error" in data


# ── Playback controls ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "endpoint, method",
    [
        ("/api/play", "put"),
        ("/api/pause", "put"),
        ("/api/next", "post"),
        ("/api/previous", "post"),
    ],
)
def test_play_pause_next_previous(client, endpoint, method):
    """Basic playback control endpoints return 204."""
    with patch("server.requests.request", return_value=_mock_response(204)):
        resp = getattr(client, method)(endpoint)
    assert resp.status_code == 204


# ── Volume ──────────────────────────────────────────────────────────

def test_volume_valid(client):
    """PUT /api/volume?volume_percent=50 succeeds."""
    with patch("server.requests.request", return_value=_mock_response(204)):
        resp = client.put("/api/volume?volume_percent=50")
    assert resp.status_code == 204


def test_volume_invalid(client):
    """PUT /api/volume?volume_percent=150 returns 400 (server-side validation)."""
    resp = client.put("/api/volume?volume_percent=150")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data


# ── Seek ────────────────────────────────────────────────────────────

def test_seek_valid(client):
    """PUT /api/seek?position_ms=5000 succeeds."""
    with patch("server.requests.request", return_value=_mock_response(204)):
        resp = client.put("/api/seek?position_ms=5000")
    assert resp.status_code == 204


def test_seek_invalid(client):
    """PUT /api/seek?position_ms=-1 returns 400 (server-side validation)."""
    resp = client.put("/api/seek?position_ms=-1")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data


# ── Shuffle ─────────────────────────────────────────────────────────

def test_shuffle_valid(client):
    """PUT /api/shuffle?state=true succeeds."""
    with patch("server.requests.request", return_value=_mock_response(204)):
        resp = client.put("/api/shuffle?state=true")
    assert resp.status_code == 204


def test_shuffle_invalid(client):
    """PUT /api/shuffle?state=banana returns 400 (server-side validation)."""
    resp = client.put("/api/shuffle?state=banana")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data


# ── Repeat ──────────────────────────────────────────────────────────

def test_repeat_valid(client):
    """PUT /api/repeat?state=track succeeds."""
    with patch("server.requests.request", return_value=_mock_response(204)):
        resp = client.put("/api/repeat?state=track")
    assert resp.status_code == 204


def test_repeat_invalid(client):
    """PUT /api/repeat?state=banana returns 400 (server-side validation)."""
    resp = client.put("/api/repeat?state=banana")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data


# ── Settings ────────────────────────────────────────────────────────

def test_settings_get(client):
    """GET /api/settings returns the default settings."""
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["screensaver_timeout_minutes"] == 10
    assert data["lyrics_enabled"] is True
    assert data["spin_speed_rpm"] == 33.333
    assert data["volume_step"] == 5
    assert data["screensaver_enabled"] is True


def test_settings_put(client):
    """PUT /api/settings updates and persists values."""
    with patch.object(
        server, "load_settings", return_value=dict(server.DEFAULT_SETTINGS)
    ), patch.object(server, "save_settings") as mock_save:
        resp = client.put(
            "/api/settings",
            data=json.dumps({"volume_step": 10}),
            content_type="application/json",
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["volume_step"] == 10
    mock_save.assert_called_once()
    saved = mock_save.call_args[0][0]
    assert saved["volume_step"] == 10


# ── Queue ───────────────────────────────────────────────────────────

def test_queue_endpoint(client):
    """GET /api/queue proxies correctly and returns queue data."""
    body = {"currently_playing": None, "queue": []}
    with patch("server.requests.request", return_value=_mock_response(200, body)):
        resp = client.get("/api/queue")

    assert resp.status_code == 200
    data = resp.get_json()
    assert "queue" in data
