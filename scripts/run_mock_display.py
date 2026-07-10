#!/usr/bin/env python3
"""Run a deterministic local display for browser and visual regression checks."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
MOCK_CONFIG = Path(tempfile.gettempdir()) / "spotify-display-mock-config.json"
MOCK_CONFIG.write_text(json.dumps({
    "client_id": "mock-client",
    "client_secret": "mock-secret",
    "security": {"session_secret": "mock-session-secret"},
    "backlight": {"enabled": False},
    "wled": {"enabled": False, "devices": []},
}), encoding="utf-8")
os.chmod(MOCK_CONFIG, 0o600)
os.environ["SPOTIFY_DISPLAY_CONFIG"] = str(MOCK_CONFIG)
os.environ["SPOTIFY_DISPLAY_DISABLE_BACKGROUND"] = "1"
os.environ["FLASK_SECRET_KEY"] = "mock-session-secret"

import server  # noqa: E402  (environment must be set before application import)


_lock = threading.Lock()
_mode = "playing"
_started = time.monotonic()
_volume = 54


def _track(track_id="mock-track-a", art="/static/mock-album.svg"):
    title = {
        "mock-track-a": "Midnight Geometry",
        "mock-track-b": "Copper Sunrise",
        "mock-track-noart": "Sleeveless Pressing",
        "mock-track-badart": "Damaged Sleeve",
    }.get(track_id, "Browser Fixture")
    return {
        "id": track_id,
        "uri": f"spotify:track:{track_id}",
        "name": title,
        "artists": [{"name": "The Test Pressings"}],
        "duration_ms": 244000,
        "album": {
            "id": "mock-album",
            "name": "Integration Sessions",
            "album_type": "album",
            "total_tracks": 8,
            "release_date": "2026-07-10",
            "label": "Local Test Records",
            "images": ([{"url": art, "width": 1080, "height": 1080}] if art else []),
        },
    }


def mock_playback_state():
    with _lock:
        mode = _mode
        elapsed = int((time.monotonic() - _started) * 1000)
        volume = _volume
    if mode == "idle":
        return None
    if mode == "error":
        return None
    track_id = "mock-track-b" if mode == "next" else "mock-track-a"
    art = "/static/mock-album-b.svg" if mode == "next" else "/static/mock-album.svg"
    if mode == "noart":
        track_id, art = "mock-track-noart", ""
    elif mode == "badart":
        track_id, art = "mock-track-badart", "/static/mock-album-missing.svg"
    playing = mode not in {"paused"}
    return {
        "is_playing": playing,
        "progress_ms": min(244000, 52000 + elapsed if playing else 52000),
        "volume_percent": volume,
        "source": "mock",
        "item": _track(track_id, art),
    }


def set_mode(mode):
    global _mode, _started
    with _lock:
        _mode = mode
        _started = time.monotonic()


class FakeResponse:
    def __init__(self, status_code=204, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


class FakeBacklight:
    def __init__(self):
        self.percent = 70
        self.active = 70
        self.mode = "active"

    def status(self, refresh=False):
        return {
            "enabled": True,
            "available": False,
            "mode": self.mode,
            "percent": self.percent,
            "desired_percent": self.percent,
            "active_percent": self.active,
            "idle_percent": 10,
            "safe_max_percent": 80,
            "pending": False,
            "error": "mock_hardware",
        }

    def set_percent(self, value):
        self.percent = max(0, min(100, int(value)))
        self.active = self.percent
        self.mode = "active"
        return self.status()

    def set_idle(self):
        self.percent = 10
        self.mode = "idle"
        return self.status()

    def set_active(self):
        self.percent = self.active
        self.mode = "active"
        return self.status()


_fake_backlight = FakeBacklight()
def mock_playback_with_availability():
    with _lock:
        unavailable = _mode == "error"
    return (None, False) if unavailable else (mock_playback_state(), True)


def mock_go_librespot_state():
    state, available = mock_playback_with_availability()
    return available, state


server.read_playback_state = mock_playback_state
server.read_playback_state_with_availability = mock_playback_with_availability
server.read_go_librespot_state = mock_go_librespot_state
server.attach_album_extras = lambda _state: None
server._get_backlight_controller = lambda: _fake_backlight
server._wled_active_devices = lambda: [{
    "name": "Mock WLED",
    "ip": "192.168.1.67",
    "port": 80,
    "pixel_count": 120,
}]


def mock_control(action):
    if action == "play-pause":
        with _lock:
            current = _mode
        set_mode("playing" if current == "paused" else "paused")
    elif action == "next":
        set_mode("next")
    elif action == "previous":
        set_mode("playing")
    return True, "ok"


server.control_playback = mock_control
server.play_uri_local = lambda *_args, **_kwargs: (True, "ok")
server.current_album_id = lambda: "mock-album"
server.lookup_album_tracks = lambda _album: [
    {"number": number, "disc": 1, "name": name, "duration_ms": 210000, "uri": f"spotify:track:mock-{number}"}
    for number, name in enumerate(
        ["Needle Drop", "Midnight Geometry", "Copper Sunrise", "Last Groove"], start=1
    )
]
server.crate_payload = lambda: {
    "sections": [
        {"id": "saved", "title": "Saved albums", "items": [
            {"id": "crate-a", "title": "Integration Sessions", "subtitle": "The Test Pressings", "uri": "spotify:album:mock-album", "image": "/static/mock-album.svg", "accent": "#dc7945"},
            {"id": "crate-b", "title": "Second Fixture", "subtitle": "Local Browser Band", "uri": "spotify:album:mock-b", "image": "/static/mock-album-b.svg", "accent": "#4c7fbd"},
        ]},
        {"id": "house", "title": "House picks", "items": []},
    ]
}
server.idle_launcher_payload = lambda include_private=True: {
    "title": "Choose a record",
    "playlists": server.crate_payload()["sections"][0]["items"],
}

for track_id, line in (
    ("mock-track-a", "Midnight geometry turns"),
    ("mock-track-b", "Copper light arrives"),
    ("mock-track-noart", "No sleeve, no stale art"),
    ("mock-track-badart", "A failed sleeve stays neutral"),
):
    server._lyrics_cache.set(track_id, {
        "syncedLyrics": f"[00:00.00]Local browser fixture\n[00:52.00]{line}\n[00:56.50]Regression checks stay in time",
        "plainLyrics": "",
        "status": "ok",
    })


def fake_get(url, *args, **kwargs):
    if url.endswith("/status"):
        return FakeResponse(200, {"volume_steps": 100, "volume": _volume})
    return FakeResponse(404, {})


server.requests.get = fake_get
server.requests.post = lambda *_args, **_kwargs: FakeResponse(204, {})


@server.app.route("/__mock/state/<mode>", methods=["POST"])
def mock_state_route(mode):
    if mode not in {"playing", "paused", "next", "noart", "badart", "idle", "error"}:
        return server.jsonify({"error": "unknown mock mode"}), 404
    set_mode(mode)
    return server.jsonify({"mode": mode})


@server.app.route("/__mock/snapshot")
def mock_snapshot_route():
    state, available = mock_playback_with_availability()
    if not available:
        return server.jsonify({"mode": "error"}), 503
    return server.jsonify(copy.deepcopy(state))


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_DISPLAY_PORT", "5105"))
    server.app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
