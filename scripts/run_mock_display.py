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
        # The id feeds current_artist() — the artist shelf hangs off it.
        "artists": [{"name": "The Test Pressings", "id": "mock-artist"}],
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
        # The kiosk gates the record crate on this signal — without it the
        # shelf degrades to the no-receiver prompt even though the fixture
        # crate below is populated.
        "profile": {"profile_state": "linked", "profile_epoch": "mock-epoch"},
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
_MOCK_ALBUM_TRACKS = {
    "mock-album": [
        # The playing fixture track IS "Midnight Geometry" — reuse its real URI
        # so the kiosk's current-row marker (EQ bars) has something to match.
        ("Needle Drop", "spotify:track:mock-1"),
        ("Midnight Geometry", "spotify:track:mock-track-a"),
        ("Copper Sunrise", "spotify:track:mock-3"),
        ("Last Groove", "spotify:track:mock-4"),
    ],
    "mock-b": [
        ("Second Stylus", "spotify:track:mock-b-1"),
        ("Cartridge Blues", "spotify:track:mock-b-2"),
        ("Anti-Skate", "spotify:track:mock-b-3"),
    ],
}
server.lookup_album_tracks = lambda album_id: [
    {"number": number, "disc": 1, "name": name, "duration_ms": 210000, "uri": uri}
    for number, (name, uri) in enumerate(_MOCK_ALBUM_TRACKS.get(album_id, []), start=1)
]

# The artist shelf: one other record by The Test Pressings, so the tracklist
# rail, re-targeted tracklists, Put it on and Stack next are all exercisable.
server.fetch_artist_albums = lambda artist_id, fallback_artist_name="": ([
    {"id": "deep-mock-b", "uri": "spotify:album:mock-b", "title": "Second Fixture",
     "subtitle": "The Test Pressings", "image": "/static/mock-album-b.svg",
     "accent": "#4c7fbd", "type": "album"},
] if artist_id == "mock-artist" else [])

# Record queue calls so browser checks can assert what got stacked.
_queued_uris = []


@server.app.route("/__mock/queued")
def mock_queued_route():
    return server.jsonify({"queued": list(_queued_uris)})
server.crate_payload = lambda: {
    "profile_state": "linked",
    "profile_epoch": "mock-epoch",
    "sections": [
        {"id": "saved", "title": "Saved albums", "items": [
            {"id": "mock-album", "title": "Integration Sessions", "subtitle": "The Test Pressings", "uri": "spotify:album:mock-album", "image": "/static/mock-album.svg", "accent": "#dc7945"},
            {"id": "crate-b", "title": "Second Fixture", "subtitle": "Local Browser Band", "uri": "spotify:album:mock-b", "image": "/static/mock-album-b.svg", "accent": "#4c7fbd"},
        ]},
        {"id": "house", "title": "House picks", "items": []},
    ]
}
server.idle_launcher_payload = lambda include_private=True: {
    "title": "Choose a record",
    "playlists": server.crate_payload()["sections"][0]["items"],
}

# Synced fixture lyrics: one line every ~8s across the whole 244s track so
# every seek position has an active line — exercises the karaoke fill sweep.
_MOCK_LYRIC_LINES = [
    "Needle down on midnight geometry",
    "Circles in the dust where the light should be",
    "Thirty-three and a third of the way to dawn",
    "Every groove a road the night drives on",
    "Copper wires humming in the wall",
    "Static like a tide in the hall",
    "Turn the label toward the lamp and read",
    "Pressed in nineteen-something, all we need",
    "Side A carries what the day forgot",
    "Side B answers whether asked or not",
    "Dust sleeve whispers when the platter slows",
    "Run-out etching only the stylus knows",
    "Spindle holds the spinning world in place",
    "Twenty minutes of recorded grace",
    "Drop the tonearm, let the silence break",
    "Every crackle is a choice we make",
    "Midnight geometry, perfect and round",
    "A circle is the shortest way back to the sound",
    "Fold the night into a paper sleeve",
    "Play it again before you leave",
    "The last groove locks and holds us here",
    "Spinning slow until the morning's clear",
    "Needle up — the room remembers how",
    "Midnight geometry, then and now",
    "Coda: let the platter drift and slow",
    "One more turn before we go",
    "One more turn before we go (again)",
    "Fade on the fifty-two second reprise",
    "Hold the sleeve up to the light and see",
    "Midnight geometry, you and me",
]
_MOCK_SYNCED = "\n".join(
    f"[{(i * 8) // 60:02d}:{(i * 8) % 60:02d}.00] {text}"
    for i, text in enumerate(_MOCK_LYRIC_LINES)
)

for _track_id in ("mock-track-a", "mock-track-b", "mock-track-noart", "mock-track-badart"):
    server._lyrics_cache.set(_track_id, {
        "syncedLyrics": _MOCK_SYNCED,
        "plainLyrics": "\n".join(_MOCK_LYRIC_LINES),
        "status": "ok",
    })


def fake_get(url, *args, **kwargs):
    if url.endswith("/status"):
        return FakeResponse(200, {"volume_steps": 100, "volume": _volume})
    if "lrclib.net" in url:
        return FakeResponse(200, {
            "syncedLyrics": _MOCK_SYNCED,
            "plainLyrics": "\n".join(_MOCK_LYRIC_LINES),
        })
    return FakeResponse(404, {})


# Outbound calls go through the pooled session since the efficiency pass —
# patch it (patching the requests module no longer intercepts anything).
def fake_post(url, *args, **kwargs):
    if url.endswith("/player/add_to_queue"):
        _queued_uris.append(((kwargs.get("json") or {}).get("uri")) or "")
    return FakeResponse(204, {})


server._http.get = fake_get
server._http.post = fake_post


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
