#!/usr/bin/env python3
"""Run a deterministic local display for browser and visual regression checks."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Every mock run owns a disposable catalog, including fidelity previews. Never
# allow a browser's Add to Vinyls action to touch the real physical inventory.
MOCK_VINYLS_DIRECTORY = Path(tempfile.mkdtemp(prefix="spotify-display-mock-vinyls-"))
MOCK_VINYLS = MOCK_VINYLS_DIRECTORY / "vinyls.json"
if os.environ.get("MOCK_VINYLS_CATALOG") == "1":
    shutil.copyfile(ROOT / "data" / "vinyls.json", MOCK_VINYLS)
else:
    MOCK_VINYLS.write_text(json.dumps({"version": 1, "title": "Vinyls", "albums": []}), encoding="utf-8")
os.environ["SPOTIFY_DISPLAY_VINYLS"] = str(MOCK_VINYLS)
MOCK_CONFIG = Path(tempfile.gettempdir()) / "spotify-display-mock-config.json"
MOCK_CONFIG.write_text(json.dumps({
    "client_id": "mock-client",
    "client_secret": "mock-secret",
    "spotify_profiles": {"version": 2, "profiles": {"mock-account": {
        "account_id": "mock-account", "display_name": "Mock listener",
        "refresh_token": "mock-refresh-token", "kind": "household",
        "connected_at": time.time(), "authorized_at": time.time(),
        "expires_at": None, "receiver_aliases": ["mock-alias"],
        "scopes": ["user-library-read"],
    }}},
    "public_base_url": f"http://127.0.0.1:{os.environ.get('MOCK_DISPLAY_PORT', '5105')}",
    "security": {"session_secret": "mock-session-secret", "owner_token": "mock-owner-token"},
    "backlight": {"enabled": False},
    "wled": {"enabled": False, "devices": []},
}), encoding="utf-8")
os.chmod(MOCK_CONFIG, 0o600)
os.environ["SPOTIFY_DISPLAY_CONFIG"] = str(MOCK_CONFIG)
os.environ["SPOTIFY_DISPLAY_DISABLE_BACKGROUND"] = "1"
os.environ["FLASK_SECRET_KEY"] = "mock-session-secret"

import server  # noqa: E402  (environment must be set before application import)

server._receiver_identity.update({"alias": "mock-alias", "active": True, "epoch": "mock-epoch"})


_lock = threading.Lock()
_mode = "playing"
_started = time.monotonic()
_volume = 54
_selected_collection = None
_selected_track = None


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
        selected_collection = _selected_collection
        selected_track = _selected_track
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
    item = _track(track_id, art)
    if selected_collection:
        item["album"]["id"] = selected_collection["uri"].split(":")[-1]
        item["album"]["name"] = selected_collection["title"]
        item["album"]["images"] = [{"url": selected_collection["image"]}]
        item["artists"] = [{"name": selected_collection["subtitle"]}]
        if selected_track:
            item.update({key: selected_track[key] for key in ("uri", "name", "duration_ms")})
            item["id"] = selected_track["uri"].split(":")[-1]
    return {
        "is_playing": playing,
        "progress_ms": min(244000, 52000 + elapsed if playing else 52000),
        "device": {"volume_percent": volume, "name": "Pi Display"},
        "source": "mock",
        "item": item,
        # The kiosk gates the record crate on this signal — without it the
        # shelf degrades to the no-receiver prompt even though the fixture
        # crate below is populated.
        "profile": server._public_profile_context(),
    }


def set_mode(mode):
    global _mode, _started, _selected_collection, _selected_track
    with _lock:
        _mode = mode
        _started = time.monotonic()
        _selected_collection = None
        _selected_track = None


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
server._request_wled_scan = lambda: False


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
server.current_album_id = lambda: (mock_playback_state() or {}).get("item", {}).get("album", {}).get("id")
_MOCK_ALBUM_TRACKS = {
    "mock-album": [
        # The playing fixture track IS "Midnight Geometry" — reuse its real URI
        # so the tracklist can match the playing row.
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
    for number, (name, uri) in enumerate(_MOCK_ALBUM_TRACKS.get(album_id, _MOCK_ALBUM_TRACKS["mock-album"]), start=1)
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

# More than one mosaic page and several grid pages, with useful artist/title
# search matches. All artwork stays local and every ID uses the mock tracklist.
MOCK_EXTRA_ALBUMS = [
    ("Paper Satellites", "Juniper Club"),
    ("Soft Focus", "Juniper Club"),
    ("Small Hours", "Velvet District"),
    ("Glasshouse", "Velvet District"),
    ("Late Light", "Northbound Quartet"),
    ("City Without Rain", "Northbound Quartet"),
    ("Open Water", "Mira Sol"),
    ("A Place to Begin", "Mira Sol"),
    ("Electric Orchard", "Juniper Club"),
    ("Window Seat", "Local Browser Band"),
    ("Red Clay", "The Test Pressings"),
    ("Night Ferry", "Northbound Quartet"),
    ("Side Streets", "Velvet District"),
    ("Quiet Thunder", "Mira Sol"),
    ("Daybreak Radio", "Juniper Club"),
    ("Nothing in a Hurry", "Northbound Quartet"),
    ("Palm Reader", "Mira Sol"),
    ("Signal and Noise", "Local Browser Band"),
    ("Warm Front", "Velvet District"),
    ("Long Way Home", "The Test Pressings"),
    ("Hidden Gardens", "Juniper Club"),
    ("The Last Tram", "Northbound Quartet"),
]


def mock_extra_albums(start, end):
    return [
        {
            "id": f"extra-{index}",
            "title": title,
            "subtitle": artist,
            "uri": f"spotify:album:mock-extra-{index}",
            "image": "/static/mock-album.svg" if index % 2 else "/static/mock-album-b.svg",
            "accent": "#dc7945" if index % 2 else "#4c7fbd",
        }
        for index, (title, artist) in enumerate(MOCK_EXTRA_ALBUMS)
        if start <= index < end
    ]


if os.environ.get("MOCK_VINYLS_CATALOG") != "1":
    MOCK_VINYLS.write_text(json.dumps({
        "version": 1, "title": "Vinyls", "albums": [
            {**item, "uri": f"spotify:album:{index + 100:022d}"}
            for index, item in enumerate(mock_extra_albums(0, 8))
        ],
    }, indent=2), encoding="utf-8")

# These are ordinary account-scoped playlist cards, not guessed production IDs.
MOCK_MADE_FOR_YOU = [
    {"id": f"made-for-you-{index}", "uri": f"spotify:playlist:{index + 900:022d}",
     "title": title, "subtitle": subtitle, "type": "playlist", "source": "made_for_you",
     "quick_kind": kind, "image": "/static/mock-album.svg" if index % 2 else "/static/mock-album-b.svg",
     "accent": "#dc7945" if index % 2 else "#4c7fbd"}
    for index, (title, subtitle, kind) in enumerate([
        ("Discover Weekly", "Fresh finds for Mock listener", "discover_weekly"),
        ("Release Radar", "New releases from your artists", "release_radar"),
        ("Daily Mix 1", "The Test Pressings, Juniper Club and more", "mix"),
        ("Daily Mix 2", "Mira Sol, Velvet District and more", "mix"),
        ("Jazz Mix", "Northbound Quartet and late-night favourites", "mix"),
    ])
]


# Manual refresh is immediate in the fixture; never start Spotify discovery jobs.
server._rebuild_crate_async = lambda context=None: None
server.crate_payload = lambda context=None: {
    **server._public_profile_context(context),
    "made_for_you": {"status": "ready", "missing": [],
                     "message": "Your Spotify discoveries and saved mixes, ready to play."},
    "sections": [
        {"id": "made_for_you", "title": "Made for you", "items": copy.deepcopy(MOCK_MADE_FOR_YOU)},
        {"id": "vinyls", "title": "Vinyls", "items": server.load_vinyls(server.VINYLS_FILE)},
        {"id": "yours", "title": "Your playlists", "items": [
            {"id": "playlist-a", "title": "Slow Sunday", "subtitle": "A little room to breathe", "uri": "spotify:playlist:mock-sunday", "image": "/static/mock-album-b.svg", "accent": "#4c7fbd"},
            {"id": "playlist-b", "title": "After Hours", "subtitle": "The lights stay low", "uri": "spotify:playlist:mock-night", "image": "/static/mock-album.svg", "accent": "#dc7945"},
        ]},
        {"id": "saved", "title": "Saved albums", "items": [
            {"id": "mock-album", "title": "Integration Sessions", "subtitle": "The Test Pressings", "uri": "spotify:album:mock-album", "image": "/static/mock-album.svg", "accent": "#dc7945"},
            {"id": "crate-b", "title": "Second Fixture", "subtitle": "Local Browser Band", "uri": "spotify:album:mock-b", "image": "/static/mock-album-b.svg", "accent": "#4c7fbd"},
        ] + mock_extra_albums(0, 12)},
        {"id": "recent", "title": "Recently spun", "items": [
            {"id": "recent-a", "title": "Copper Sunrise", "subtitle": "The Test Pressings", "uri": "spotify:album:mock-copper", "image": "/static/mock-album.svg", "accent": "#dc7945"},
        ]},
        {"id": "deeper", "title": "Deeper cuts", "items": [
            {"id": "deeper-a", "title": "B-Side Stories", "subtitle": "The Test Pressings", "uri": "spotify:album:mock-bside", "image": "/static/mock-album-b.svg", "accent": "#4c7fbd"},
            {"id": "deeper-b", "title": "The Blue Room", "subtitle": "Local Browser Band", "uri": "spotify:album:mock-blue", "image": "/static/mock-album-b.svg", "accent": "#4c7fbd"},
        ] + mock_extra_albums(12, len(MOCK_EXTRA_ALBUMS))},
        {"id": "house", "title": "House picks", "items": [
            {"id": "house-a", "title": "The Listening Room", "subtitle": "Records for right now", "uri": "spotify:playlist:mock-house", "image": "/static/mock-album.svg", "accent": "#dc7945"},
        ]},
    ]
}
server.idle_launcher_payload = lambda include_private=True: {
    "title": "Choose a record",
    "playlists": next(section["items"] for section in server.crate_payload()["sections"] if section["id"] == "vinyls"),
}


# A valid Spotify-shaped ID, deliberately absent from the copied/default
# catalog. Search and verification use local fixtures; no Spotify call occurs.
_known_vinyl_uris = {item["uri"] for item in server.load_vinyls(server.VINYLS_FILE)}
MOCK_ADDITION_ID = next(
    f"mockVinylAddition{index:05d}" for index in range(1000)
    if f"spotify:album:mockVinylAddition{index:05d}" not in _known_vinyl_uris
)
MOCK_ADDITION_ALBUM = {
    "id": MOCK_ADDITION_ID, "uri": f"spotify:album:{MOCK_ADDITION_ID}",
    "title": "The Blue Hour", "subtitle": "The Test Pressings",
    "image": "/static/mock-album-b.svg", "type": "album",
    "release_date": "2026-09-12", "release_date_precision": "day",
    "album_type": "album", "total_tracks": 5, "label": "Local Test Records",
}
_MOCK_ALBUM_TRACKS[MOCK_ADDITION_ID] = [
    (title, f"spotify:track:{index + 7100:022d}")
    for index, title in enumerate(["First Light", "The Blue Hour", "Slow Motion", "Afterglow", "Home Again"])
]


def mock_lookup_album(album_id):
    if album_id == MOCK_ADDITION_ID:
        return copy.deepcopy(MOCK_ADDITION_ALBUM)
    uri = f"spotify:album:{album_id}"
    item = server._crate_item(server.crate_payload(), uri)
    if item is None:
        return None
    with open(server.VINYLS_FILE, encoding="utf-8") as source:
        catalog = json.load(source)
    stored = next((entry for entry in catalog["albums"] if entry.get("uri") == uri), None)
    metadata = {}
    if stored and os.environ.get("MOCK_VINYLS_CATALOG") == "1":
        # Imported facts remain faithful to the copied physical inventory.
        release = stored.get("spotify_release_date")
        if isinstance(release, str) and len(release) in (4, 7, 10):
            metadata.update(release_date=release, release_date_precision={4: "year", 7: "month", 10: "day"}[len(release)])
        if isinstance(stored.get("spotify_total_tracks"), int):
            metadata["total_tracks"] = stored["spotify_total_tracks"]
    else:
        metadata = {"release_date": "2026-07-10", "release_date_precision": "day",
                    "album_type": "album", "total_tracks": len(server.lookup_album_tracks(album_id)),
                    "label": "Local Test Records"}
    return {**item, **metadata, "id": album_id, "type": "album"}


def mock_lookup_album_metadata(album_id, tracks=None):
    metadata = server._sanitize_album_metadata(mock_lookup_album(album_id))
    # Track timings in this fixture are synthetic. Only display a mock running
    # time for the synthetic albums, not for photographed real records.
    if (os.environ.get("MOCK_VINYLS_CATALOG") != "1" or album_id == MOCK_ADDITION_ID) and isinstance(tracks, list) and len(tracks) == metadata.get("total_tracks"):
        metadata["duration_ms"] = sum(track["duration_ms"] for track in tracks)
    return metadata


server.lookup_album = mock_lookup_album
server.lookup_album_metadata = mock_lookup_album_metadata
server.get_client_token = lambda: "mock-client-token"


def mock_play_uri(uri, skip_to_uri=None):
    global _selected_collection, _selected_track, _mode, _started
    item = server._crate_item(server.crate_payload(), uri)
    if item is None:
        return False, "Collection is not in the mock crate"
    tracks = server.lookup_album_tracks(uri.split(":")[-1])
    selected = next((track for track in tracks if track["uri"] == skip_to_uri), tracks[0])
    with _lock:
        _selected_collection = dict(item)
        _selected_track = selected
        _mode = "playing"
        _started = time.monotonic()
    return True, "ok"


server.play_uri_local = mock_play_uri

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
    if url == f"{server.SPOTIFY_API_BASE}/search":
        query = str((kwargs.get("params") or {}).get("q", "")).casefold()
        haystack = f"{MOCK_ADDITION_ALBUM['title']} {MOCK_ADDITION_ALBUM['subtitle']}".casefold()
        matches = all(word in haystack for word in query.split())
        # Public search only accepts Spotify-hosted images. An empty search
        # image gives the normal cover fallback without any remote art request;
        # verified lookup/addition supplies the local sleeve above.
        items = [{"id": MOCK_ADDITION_ID, "name": MOCK_ADDITION_ALBUM["title"],
                  "artists": [{"name": MOCK_ADDITION_ALBUM["subtitle"]}],
                  "images": [], "release_date": MOCK_ADDITION_ALBUM["release_date"]}] if matches else []
        return FakeResponse(200, {"albums": {"items": items}})
    if url.endswith("/status"):
        return FakeResponse(200, {"volume_steps": 100, "volume": _volume})
    return FakeResponse(404, {})


# Outbound calls go through the pooled session since the efficiency pass —
# patch it (patching the requests module no longer intercepts anything).
def fake_post(url, *args, **kwargs):
    global _volume
    if url.endswith("/player/volume"):
        with _lock:
            _volume = max(0, min(100, int((kwargs.get("json") or {}).get("volume", _volume))))
    if url.endswith("/player/add_to_queue"):
        _queued_uris.append(((kwargs.get("json") or {}).get("uri")) or "")
        return FakeResponse(200, {})
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
