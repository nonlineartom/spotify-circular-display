#!/usr/bin/env python3
"""Flask server — Spotify Connect display.

Display: go-librespot's local API is preferred for playback state and controls.
Raspotify's --onevent state file remains as a fallback for older installs.
Track metadata can also be enriched via Spotify client credentials.

Controls: the Pi's touch controls call the local Spotify Connect receiver API.
The legacy Spotify Web API OAuth path is retained only as a fallback.
"""

import concurrent.futures
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.parse
import requests
from flask import Flask, request, render_template, jsonify, redirect

app = Flask(__name__)
app.secret_key = os.urandom(24)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
IDLE_PLAYLISTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "idle_playlists.json")
RECENT_SPINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recently_spun.json")
IDLE_PLAYLISTS_EXAMPLE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "idle_playlists.example.json")
STATE_FILE = "/tmp/spotify-state.json"
GO_LIBRESPOT_API_BASE = os.environ.get("GO_LIBRESPOT_API_BASE", "http://127.0.0.1:3678").rstrip("/")

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SCOPES = "user-modify-playback-state user-read-playback-state"
PLAYLIST_SCOPES = "playlist-read-private user-library-read user-read-playback-state user-modify-playback-state"

# Raspotify/librespot events can occasionally be missed during Wi-Fi drops or
# Spotify handoffs. These guards keep an old "playing" event from looking alive
# forever, while still allowing normal long tracks to run from their timestamp.
PAUSED_IDLE_AFTER_SECONDS = 5 * 60
PLAYING_UNKNOWN_DURATION_STALE_SECONDS = 30 * 60
END_OF_TRACK_GRACE_SECONDS = 8
STOPPED_IDLE_EVENTS = {
    "stopped",
    "end_of_track",
    "unavailable",
    "session_disconnected",
    "network_down",
}

# ── In-memory caches ────────────────────────────────────────

_client_token = None
_client_token_expiry = 0
_user_token = None
_user_token_expiry = 0
_track_cache = {}  # track_id -> {name, artists, album, images, duration_ms}
_playlist_cache = {"loaded_at": 0, "items": []}
_album_cache = {}  # album_id -> {"label": str}
_uri_image_cache = {}   # spotify uri -> resolved cover art url
_uri_image_failed = {}  # spotify uri -> wall time of last failed resolve
_artist_albums_cache = {}  # artist_id -> [crate item dicts]
_recent_spins = {"loaded": False, "items": []}  # newest first
_recent_spins_lock = threading.Lock()
_last_spin_album = None
_crate_cache = {"built_at": 0, "payload": None}
_enrich_inflight = set()  # track_ids with a background enrichment thread running
_enrich_last_attempt = {}  # track_id -> wall time of last enrichment attempt
_enrich_lock = threading.Lock()

# WLED discovery cache: ip -> {"name": str, "ip": str, "port": int, "last_seen": float}
_wled_devices = {}
_wled_devices_lock = threading.Lock()
WLED_DEVICE_TTL_SECONDS = 60


def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def resolve_uri_image(uri):
    """Best-effort cover art for a spotify:{type}:{id} URI via client credentials.

    Lets idle_playlists.json entries omit the manual "image" field — the kiosk
    shelf still gets real sleeves. Successes cache forever; failures back off
    for 10 minutes.
    """
    if uri in _uri_image_cache:
        return _uri_image_cache[uri]
    if time.time() - _uri_image_failed.get(uri, 0) < 600:
        return ""

    parts = uri.split(":")
    if len(parts) != 3:
        return ""
    endpoint = {
        "playlist": f"/playlists/{parts[2]}",
        "album": f"/albums/{parts[2]}",
        "artist": f"/artists/{parts[2]}",
        "track": f"/tracks/{parts[2]}",
    }.get(parts[1])
    if not endpoint:
        return ""

    token = get_client_token()
    if not token:
        return ""

    try:
        resp = requests.get(
            f"{SPOTIFY_API_BASE}{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 200:
            _uri_image_failed[uri] = time.time()
            return ""
        data = resp.json()
        images = data.get("images") or (data.get("album") or {}).get("images") or []
        url = images[0].get("url", "") if images else ""
        _uri_image_cache[uri] = url
        return url
    except Exception as e:
        print(f"Cover art resolve failed for {uri}: {e}")
        _uri_image_failed[uri] = time.time()
        return ""


def load_idle_playlists():
    """Load configured idle launcher playlists.

    The local idle launcher is deliberately config driven so the display can work
    without requiring guests to authenticate first.
    """
    global _playlist_cache
    if _playlist_cache["items"] and time.time() - _playlist_cache["loaded_at"] < 30:
        return _playlist_cache["items"]

    source = IDLE_PLAYLISTS_FILE if os.path.exists(IDLE_PLAYLISTS_FILE) else IDLE_PLAYLISTS_EXAMPLE_FILE
    try:
        with open(source, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"playlists": []}

    playlists = []
    for idx, item in enumerate(data.get("playlists", [])):
        uri = item.get("uri", "")
        if not uri.startswith("spotify:"):
            continue
        playlists.append({
            "id": f"house-{idx}",
            "title": item.get("title", "Playlist"),
            "subtitle": item.get("subtitle", "House pick"),
            "uri": uri,
            "image": item.get("image", "") or resolve_uri_image(uri),
            "accent": item.get("accent", "#ffffff"),
        })

    _playlist_cache = {"loaded_at": time.time(), "items": playlists}
    return playlists


def get_client_token():
    """Get a Spotify app-level token via client credentials flow.

    This does NOT require a user to log in — only the app's
    client_id and client_secret are needed.
    """
    global _client_token, _client_token_expiry

    if _client_token and _client_token_expiry > time.time() + 60:
        return _client_token

    config = load_config()
    client_id = config.get("client_id", "")
    client_secret = config.get("client_secret", "")

    if not client_id or not client_secret:
        return None

    try:
        resp = requests.post(SPOTIFY_TOKEN_URL, data={
            "grant_type": "client_credentials",
        }, auth=(client_id, client_secret), timeout=5)

        if resp.status_code != 200:
            print(f"Client credentials error: {resp.status_code} {resp.text}")
            return None

        data = resp.json()
        _client_token = data["access_token"]
        _client_token_expiry = time.time() + data.get("expires_in", 3600)
        return _client_token

    except Exception as e:
        print(f"Client credentials request failed: {e}")
        return None


def lookup_track(track_id):
    """Look up track metadata from Spotify using client credentials token.

    Results are cached in memory by track_id to avoid repeated API calls.
    """
    if not track_id:
        return None

    if track_id in _track_cache:
        return _track_cache[track_id]

    token = get_client_token()
    if not token:
        return None

    try:
        resp = requests.get(
            f"{SPOTIFY_API_BASE}/tracks/{track_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 200:
            print(f"Track lookup error for {track_id}: {resp.status_code}")
            return None

        data = resp.json()
        track_info = {
            "id": data.get("id", track_id),
            "name": data.get("name", "Unknown Track"),
            "duration_ms": data.get("duration_ms", 0),
            "artists": [{"name": a.get("name", ""), "id": a.get("id", "")} for a in data.get("artists", [])],
            "album": {
                "id": data.get("album", {}).get("id", ""),
                "name": data.get("album", {}).get("name", ""),
                "images": data.get("album", {}).get("images", []),
                # 'single' drives the kiosk's 45 RPM mode; release_date feeds
                # the procedural record label.
                "album_type": data.get("album", {}).get("album_type", ""),
                "release_date": data.get("album", {}).get("release_date", ""),
            },
        }
        _track_cache[track_id] = track_info
        return track_info

    except Exception as e:
        print(f"Track lookup failed for {track_id}: {e}")
        return None


def lookup_album(album_id):
    """Look up album-level metadata (record label) via client credentials.

    Cached forever by album_id; failures cache an empty dict so we don't
    hammer the API for albums it can't serve.
    """
    if not album_id:
        return None
    if album_id in _album_cache:
        return _album_cache[album_id]

    token = get_client_token()
    if not token:
        return None

    try:
        resp = requests.get(
            f"{SPOTIFY_API_BASE}/albums/{album_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 200:
            print(f"Album lookup error for {album_id}: {resp.status_code}")
            _album_cache[album_id] = {}
            return None
        info = {"label": resp.json().get("label", "")}
        _album_cache[album_id] = info
        return info
    except Exception as e:
        print(f"Album lookup failed for {album_id}: {e}")
        return None


def queue_track_enrichment(track_id):
    """Fetch track/album extras on a background thread.

    /api/now-playing is polled every 2s by both the kiosk and wled_sync, so it
    must never block on Spotify API calls. First sighting of a track queues
    this job; the extras appear in the payload on a later poll once cached.
    """
    now = time.time()
    with _enrich_lock:
        if track_id in _enrich_inflight:
            return
        if now - _enrich_last_attempt.get(track_id, 0) < 60:
            return
        _enrich_last_attempt[track_id] = now
        _enrich_inflight.add(track_id)

    def _job():
        try:
            info = lookup_track(track_id)
            album_id = ((info or {}).get("album") or {}).get("id")
            if album_id and album_id not in _album_cache:
                lookup_album(album_id)
        finally:
            with _enrich_lock:
                _enrich_inflight.discard(track_id)

    threading.Thread(target=_job, daemon=True).start()


def _load_recent_spins():
    with _recent_spins_lock:
        if _recent_spins["loaded"]:
            return
        try:
            with open(RECENT_SPINS_FILE, "r") as f:
                _recent_spins["items"] = json.load(f).get("items", [])
        except (FileNotFoundError, json.JSONDecodeError):
            _recent_spins["items"] = []
        _recent_spins["loaded"] = True


def record_spin(item, cached_album):
    """Remember an album the display has played — the 'Recently spun' crate.

    Called from the now-playing path once enrichment metadata is cached, so
    it never adds latency. Local-first: persists to a gitignored JSON file.
    """
    global _last_spin_album
    album_id = cached_album.get("id")
    if not album_id or album_id == _last_spin_album:
        return
    _last_spin_album = album_id

    album = item.get("album") or {}
    images = album.get("images") or []
    artists = item.get("artists") or []
    entry = {
        "id": f"spin-{album_id}",
        "uri": f"spotify:album:{album_id}",
        "title": album.get("name", ""),
        "subtitle": ", ".join(a.get("name", "") for a in artists if a.get("name")),
        "image": images[0].get("url", "") if images else "",
        "accent": "#d8b96a",
        "type": "album",
        "artist_ids": [a.get("id") for a in (cached_album.get("artists") or []) if a.get("id")],
        "ts": time.time(),
    }

    _load_recent_spins()
    with _recent_spins_lock:
        items = [e for e in _recent_spins["items"] if e.get("uri") != entry["uri"]]
        items.insert(0, entry)
        _recent_spins["items"] = items[:40]
        try:
            with open(RECENT_SPINS_FILE, "w") as f:
                json.dump({"items": _recent_spins["items"]}, f)
        except OSError as e:
            print(f"Could not persist recent spins: {e}")


def recent_spin_items():
    _load_recent_spins()
    with _recent_spins_lock:
        return [dict(e) for e in _recent_spins["items"]]


def fetch_artist_albums(artist_id, fallback_artist_name=""):
    """Albums by an artist via client credentials — fuels the 'Deeper cuts'
    crate. Cached forever per artist."""
    if not artist_id:
        return []
    if artist_id in _artist_albums_cache:
        return _artist_albums_cache[artist_id]

    token = get_client_token()
    if not token:
        return []

    try:
        resp = requests.get(
            f"{SPOTIFY_API_BASE}/artists/{artist_id}/albums",
            params={"include_groups": "album", "limit": 10},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 200:
            _artist_albums_cache[artist_id] = []
            return []
        items = []
        for a in resp.json().get("items", []):
            images = a.get("images") or []
            artist = ", ".join(x.get("name", "") for x in (a.get("artists") or [])) or fallback_artist_name
            items.append({
                "id": f"deep-{a.get('id', '')}",
                "uri": a.get("uri", ""),
                "title": a.get("name", ""),
                "subtitle": artist,
                "image": images[0].get("url", "") if images else "",
                "accent": "#8a6fd1",
                "type": "album",
            })
        _artist_albums_cache[artist_id] = items
        return items
    except requests.RequestException as e:
        print(f"Artist albums fetch failed for {artist_id}: {e}")
        return []


def deeper_cut_items():
    """Discographies of recently spun artists, minus what's already in the
    recent crate — dig deeper into the artists this house actually plays."""
    spins = recent_spin_items()
    seen_albums = {e["uri"] for e in spins}
    artist_ids = []
    for e in spins:
        for aid in e.get("artist_ids") or []:
            if aid and aid not in artist_ids:
                artist_ids.append(aid)
        if len(artist_ids) >= 6:
            break

    items = []
    for aid in artist_ids[:6]:
        for album in fetch_artist_albums(aid):
            if album["uri"] in seen_albums:
                continue
            seen_albums.add(album["uri"])
            items.append(album)
    return items[:24]


def crate_payload():
    """All browsable music, in sections, for the kiosk crate UI."""
    now = time.time()
    if _crate_cache["payload"] and now - _crate_cache["built_at"] < 120:
        return _crate_cache["payload"]

    # Ordered by how good the first impression is: personal playlists lead
    # (always have covers), then the local listening history, then the
    # dig-deeper albums. House picks last — editorial-playlist covers can't
    # be resolved by newer API apps, so they may render as blank sleeves.
    sections = []
    yours = fetch_user_playlists(limit=50)
    if yours:
        sections.append({"id": "yours", "title": "Your playlists", "items": yours})
    saved = fetch_saved_albums(limit=50)
    if saved:
        sections.append({"id": "saved", "title": "Your albums", "items": saved})
    spins = recent_spin_items()
    if spins:
        sections.append({"id": "recent", "title": "Recently spun", "items": spins})
    deeper = deeper_cut_items()
    if deeper:
        sections.append({"id": "deeper", "title": "Deeper cuts", "items": deeper})
    house = load_idle_playlists()
    if house:
        sections.append({"id": "house", "title": "House picks", "items": house})

    payload = {"sections": sections}
    _crate_cache["payload"] = payload
    _crate_cache["built_at"] = now
    return payload


def attach_album_extras(state):
    """Merge cached album extras (album_type, release_date, label) into a
    now-playing payload.

    go-librespot's local API doesn't expose these, so they come from the
    client-credentials metadata cache. Missing extras queue a background
    fetch rather than blocking the response.
    """
    item = state.get("item") or {}
    track_id = item.get("id")
    if not track_id:
        return

    cached = _track_cache.get(track_id)
    if cached is None:
        queue_track_enrichment(track_id)
        return

    album = item.setdefault("album", {})
    cached_album = cached.get("album") or {}
    for key in ("album_type", "release_date"):
        if cached_album.get(key) and not album.get(key):
            album[key] = cached_album[key]

    # Feed the 'Recently spun' crate. The artist ids live on the cached
    # track, not the album — pass them along for the deeper-cuts crate.
    if state.get("is_playing"):
        record_spin(item, {**cached_album, "artists": cached.get("artists") or []})

    album_id = cached_album.get("id")
    if album_id:
        extra = _album_cache.get(album_id)
        if extra is None:
            queue_track_enrichment(track_id)
        elif extra.get("label") and not album.get("label"):
            album["label"] = extra["label"]


def fetch_user_playlists(limit=6):
    """Fetch playlists for the currently authorized Spotify user, if present."""
    token = get_user_token()
    if not token:
        return []

    try:
        resp = requests.get(
            f"{SPOTIFY_API_BASE}/me/playlists",
            params={"limit": min(50, limit), "offset": 0},
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
    except requests.RequestException as e:
        print(f"User playlist lookup failed: {e}")
        return []

    if resp.status_code != 200:
        print(f"User playlist lookup error: {resp.status_code}")
        return []

    playlists = []
    for idx, item in enumerate(resp.json().get("items", [])):
        uri = item.get("uri", "")
        if not uri.startswith("spotify:"):
            continue
        owner = item.get("owner", {}).get("display_name") or "Your playlist"
        images = item.get("images") or []
        playlists.append({
            "id": f"user-{idx}",
            "title": item.get("name", "Playlist"),
            "subtitle": owner,
            "uri": uri,
            "image": images[0].get("url", "") if images else "",
            "accent": "#1db954",
            "source": "user",
        })
    return playlists


def fetch_saved_albums(limit=50):
    """The user's saved Spotify albums — needs the user-library-read scope.

    Returns [] quietly until the OAuth token has been re-granted with that
    scope (403 before then), so the crate simply omits the section.
    """
    token = get_user_token()
    if not token:
        return []

    try:
        resp = requests.get(
            f"{SPOTIFY_API_BASE}/me/albums",
            params={"limit": min(50, limit), "offset": 0},
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
    except requests.RequestException as e:
        print(f"Saved albums lookup failed: {e}")
        return []

    if resp.status_code != 200:
        if resp.status_code != 403:  # 403 just means the scope isn't granted yet
            print(f"Saved albums lookup error: {resp.status_code}")
        return []

    albums = []
    for idx, entry in enumerate(resp.json().get("items", [])):
        album = entry.get("album") or {}
        uri = album.get("uri", "")
        if not uri.startswith("spotify:"):
            continue
        images = album.get("images") or []
        artist = ", ".join(a.get("name", "") for a in (album.get("artists") or []))
        albums.append({
            "id": f"saved-{idx}",
            "title": album.get("name", "Album"),
            "subtitle": artist,
            "uri": uri,
            "image": images[0].get("url", "") if images else "",
            "accent": "#4cb8a4",
            "type": "album",
        })
    return albums


def idle_launcher_payload():
    user_playlists = fetch_user_playlists()
    house_playlists = load_idle_playlists()
    if user_playlists:
        playlists = (user_playlists + house_playlists)[:6]
        title = "Your playlists"
    else:
        playlists = house_playlists[:6]
        title = "House picks"
    return {"playlists": playlists, "title": title}


def spotify_uri_id(uri):
    if not uri:
        return None
    parts = uri.split(":")
    if len(parts) == 3 and parts[0] == "spotify":
        return parts[2]
    return uri


def read_go_librespot_state():
    """Read playback state from go-librespot's local API.

    Returns (available, state). If the API is reachable but there is no active
    session, available is True and state is None, preventing stale fallback data
    from an old Raspotify state file from showing on the display.
    """
    try:
        resp = requests.get(f"{GO_LIBRESPOT_API_BASE}/status", timeout=0.8)
    except requests.RequestException:
        return False, None

    if resp.status_code == 204:
        return True, None
    if resp.status_code != 200:
        print(f"go-librespot status error: {resp.status_code}")
        return False, None

    try:
        status = resp.json()
    except ValueError:
        return False, None

    track = status.get("track")
    if status.get("stopped") or not track:
        return True, None

    uri = track.get("uri", "")
    track_id = spotify_uri_id(uri)
    artists = [{"name": name} for name in track.get("artist_names", [])]
    cover_url = track.get("album_cover_url")
    images = [{"url": cover_url}] if cover_url else []
    duration = track.get("duration") or 0
    position = track.get("position") or 0
    volume_steps = status.get("volume_steps") or 100
    volume = status.get("volume") or 0

    try:
        volume_percent = int(round((volume / max(volume_steps, 1)) * 100))
    except TypeError:
        volume_percent = 50

    return True, {
        "is_playing": not bool(status.get("paused")) and not bool(status.get("buffering")),
        "progress_ms": position,
        "item": {
            "id": track_id,
            "uri": uri,
            "name": track.get("name", "Unknown Track"),
            "duration_ms": duration,
            "artists": artists or [{"name": ""}],
            "album": {
                "name": track.get("album_name", ""),
                "images": images,
            },
        },
        "device": {
            "id": status.get("device_id"),
            "name": status.get("device_name", "Pi Display"),
            "volume_percent": max(0, min(100, volume_percent)),
        },
        "source": {
            "backend": "go-librespot",
            "play_origin": status.get("play_origin"),
        },
    }


def read_raspotify_playback_state():
    """Read the state file written by onevent.sh and merge with cached metadata.

    Returns a dict matching the Spotify /me/player response shape that the
    frontend already expects.
    """
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    track_id = state.get("track_id")
    if not track_id:
        return None

    now = time.time()
    timestamp = state.get("timestamp") or 0
    age = max(0, now - timestamp) if timestamp else float("inf")
    event = state.get("event", "")
    is_playing = bool(state.get("is_playing", False))

    if event in STOPPED_IDLE_EVENTS:
        return None

    # Check for stale state — if no event for 5 minutes and not playing, treat as idle
    if age > PAUSED_IDLE_AFTER_SECONDS and not is_playing:
        return None

    # Look up track metadata
    track_info = lookup_track(track_id)
    if not track_info:
        # Return minimal info without metadata
        track_info = {
            "id": track_id,
            "name": "Loading...",
            "duration_ms": state.get("duration_ms", 0),
            "artists": [{"name": ""}],
            "album": {"name": "", "images": []},
        }

    duration = track_info.get("duration_ms") or state.get("duration_ms", 0)

    # Interpolate position if playing. If the most recent event says "playing"
    # but the timestamp is older than the remaining track duration plus a small
    # grace period, assume the stop/change event was lost and stop the animation.
    position_ms = state.get("position_ms", 0)
    stale_reason = None
    if is_playing and timestamp:
        if duration > 0:
            remaining_ms = max(0, duration - position_ms)
            if age * 1000 > remaining_ms + END_OF_TRACK_GRACE_SECONDS * 1000:
                is_playing = False
                position_ms = duration
                stale_reason = "past_expected_track_end"
        elif age > PLAYING_UNKNOWN_DURATION_STALE_SECONDS:
            is_playing = False
            stale_reason = "playing_state_too_old"

    if is_playing and timestamp:
        elapsed = (now - timestamp) * 1000
        position_ms = int(position_ms + elapsed)
        if duration > 0:
            position_ms = min(position_ms, duration)

    # Build response matching Spotify /me/player shape
    return {
        "is_playing": is_playing,
        "progress_ms": position_ms,
        "item": {
            "id": track_info["id"],
            "name": track_info["name"],
            "duration_ms": duration,
            "artists": track_info["artists"],
            "album": track_info["album"],
        },
        "device": {
            "volume_percent": state.get("volume_percent", 50),
        },
        "source": {
            "event": event,
            "age_seconds": None if age == float("inf") else round(age, 1),
            "stale_reason": stale_reason,
        },
    }


def read_playback_state():
    go_available, go_state = read_go_librespot_state()
    if go_available:
        return go_state
    return read_raspotify_playback_state()


def get_user_token():
    """Get a user-level Spotify token using stored refresh_token."""
    global _user_token, _user_token_expiry

    if _user_token and _user_token_expiry > time.time() + 60:
        return _user_token

    config = load_config()
    refresh_token = config.get("refresh_token")
    if not refresh_token:
        return None

    client_id = config.get("client_id", "")
    client_secret = config.get("client_secret", "")

    try:
        resp = requests.post(SPOTIFY_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, auth=(client_id, client_secret), timeout=5)

        if resp.status_code != 200:
            print(f"User token refresh error: {resp.status_code}")
            return None

        data = resp.json()
        _user_token = data["access_token"]
        _user_token_expiry = time.time() + data.get("expires_in", 3600)

        # Store new refresh token if rotated
        if "refresh_token" in data and data["refresh_token"] != refresh_token:
            config["refresh_token"] = data["refresh_token"]
            save_config(config)

        return _user_token
    except Exception as e:
        print(f"User token refresh failed: {e}")
        return None


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_local_ip():
    """Return the LAN IP reachable by phones on the same network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_public_base_url():
    config = load_config()
    configured = os.environ.get("PUBLIC_BASE_URL") or config.get("public_base_url")
    if configured:
        return configured.rstrip("/")
    return f"http://{get_local_ip()}:5000"


def get_oauth_redirect_uri(config=None):
    """Return the exact Spotify OAuth redirect URI used for login and token exchange."""
    config = config or load_config()
    configured = os.environ.get("SPOTIFY_REDIRECT_URI") or config.get("redirect_uri")
    if configured:
        return configured
    return f"{get_public_base_url()}/callback"


def control_playback_local(action):
    """Control playback through go-librespot's local API."""
    paths = {
        "next": "/player/next",
        "previous": "/player/prev",
        "play-pause": "/player/playpause",
    }
    path = paths.get(action)
    if not path:
        return False, "Unknown action"

    try:
        resp = requests.post(f"{GO_LIBRESPOT_API_BASE}{path}", timeout=1.5)
    except requests.RequestException as e:
        return False, f"Local player API unavailable: {e}"

    if resp.status_code == 200:
        return True, "ok"
    if resp.status_code == 204:
        return False, "No active local player session"
    return False, f"Local player API error: {resp.status_code}"


def play_uri_local(uri):
    """Start playback of a Spotify URI through go-librespot's local API."""
    if not uri or not uri.startswith("spotify:"):
        return False, "Invalid Spotify URI"

    try:
        resp = requests.post(
            f"{GO_LIBRESPOT_API_BASE}/player/play",
            json={"uri": uri},
            timeout=2.5,
        )
    except requests.RequestException as e:
        return False, f"Local player API unavailable: {e}"

    if resp.status_code == 200:
        return True, "ok"
    if resp.status_code == 204:
        return False, "Local player is not ready yet"
    return False, f"Local player API error: {resp.status_code}"


def control_playback_web_api(action):
    """Legacy Spotify Web API fallback (requires a stored user token)."""
    token = get_user_token()
    if not token:
        return False, "No Spotify Web API token configured"

    headers = {"Authorization": f"Bearer {token}"}
    try:
        if action == "next":
            r = requests.post(f"{SPOTIFY_API_BASE}/me/player/next", headers=headers, timeout=5)
        elif action == "previous":
            r = requests.post(f"{SPOTIFY_API_BASE}/me/player/previous", headers=headers, timeout=5)
        elif action == "play-pause":
            # Check current state to toggle
            state_resp = requests.get(f"{SPOTIFY_API_BASE}/me/player", headers=headers, timeout=5)
            if state_resp.status_code == 200:
                is_playing = state_resp.json().get("is_playing", False)
                if is_playing:
                    r = requests.put(f"{SPOTIFY_API_BASE}/me/player/pause", headers=headers, timeout=5)
                else:
                    r = requests.put(f"{SPOTIFY_API_BASE}/me/player/play", headers=headers, timeout=5)
            else:
                return False, f"Could not read player state: {state_resp.status_code}"
        else:
            return False, "Unknown action"

        if r.status_code in (200, 202, 204):
            return True, "ok"
        return False, f"Spotify API error: {r.status_code}"
    except Exception as e:
        return False, str(e)


def control_playback(action):
    """Control playback using the local receiver, with Web API fallback."""
    ok, msg = control_playback_local(action)
    if ok:
        return True, msg

    # If an owner has already configured OAuth, keep supporting it as a fallback.
    if get_user_token():
        return control_playback_web_api(action)

    return False, msg


# ── UI routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/connect")
def connect():
    """Mobile-friendly page explaining how to connect to Pi Display."""
    return render_template("connect.html")


@app.route("/join")
def join():
    """Phone-friendly entry point for future guest personalization."""
    return render_template("join.html")


@app.route("/login")
def login():
    """Legacy one-time OAuth fallback for Spotify Web API controls."""
    config = load_config()
    client_id = config.get("client_id", "")
    if not client_id:
        return "Spotify client_id is missing from config.json", 500
    scope = PLAYLIST_SCOPES if request.args.get("playlist") else SCOPES
    redirect_uri = get_oauth_redirect_uri(config)
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
    })
    return redirect(f"{SPOTIFY_AUTH_URL}?{params}")


@app.route("/callback")
def callback():
    """Legacy OAuth callback — stores refresh token for Web API fallback."""
    code = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        return f"Authorization failed: {error or 'no code'}", 400

    config = load_config()
    redirect_uri = get_oauth_redirect_uri(config)

    try:
        resp = requests.post(SPOTIFY_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }, auth=(config["client_id"], config["client_secret"]), timeout=10)

        if resp.status_code != 200:
            return f"Token exchange failed: {resp.status_code}", 500

        data = resp.json()
        config["refresh_token"] = data["refresh_token"]
        save_config(config)

        global _user_token, _user_token_expiry
        _user_token = data["access_token"]
        _user_token_expiry = time.time() + data.get("expires_in", 3600)

        return redirect("/connect?auth=ok")
    except Exception as e:
        return f"Error: {e}", 500


# ── API routes ───────────────────────────────────────────────

@app.route("/api/now-playing")
def now_playing():
    """Return current playback state from the local Spotify Connect receiver."""
    state = read_playback_state()
    if state is None:
        return "", 204  # No content — nothing playing
    attach_album_extras(state)
    return jsonify(state)


@app.route("/api/health")
def health():
    """Return local receiver and fallback event health for troubleshooting."""
    go_available, go_state = read_go_librespot_state()

    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = None
        state_error = "state_file_missing"
    except json.JSONDecodeError:
        state = None
        state_error = "state_file_invalid"
    else:
        state_error = None

    if state is None:
        return jsonify({
            "ok": go_available,
            "go_librespot": {
                "available": go_available,
                "active": go_state is not None,
                "api_base": GO_LIBRESPOT_API_BASE,
            },
            "raspotify_state": {
                "ok": False,
                "reason": state_error,
                "path": STATE_FILE,
            },
        }), 200 if go_available else 503

    timestamp = state.get("timestamp") or 0
    age = max(0, time.time() - timestamp) if timestamp else None
    return jsonify({
        "ok": go_available or bool(state.get("track_id")),
        "go_librespot": {
            "available": go_available,
            "active": go_state is not None,
            "api_base": GO_LIBRESPOT_API_BASE,
        },
        "raspotify_state": {
            "ok": True,
            "path": STATE_FILE,
            "event": state.get("event", ""),
            "track_id": state.get("track_id"),
            "is_playing": bool(state.get("is_playing", False)),
            "position_ms": state.get("position_ms"),
            "duration_ms": state.get("duration_ms"),
            "volume_percent": state.get("volume_percent"),
            "age_seconds": None if age is None else round(age, 1),
        },
    })


@app.route("/api/control/<action>", methods=["POST"])
def control(action):
    """Control playback (next, previous, play-pause)."""
    if action not in ("next", "previous", "play-pause"):
        return jsonify({"error": "Invalid action"}), 400
    ok, msg = control_playback(action)
    if ok:
        return jsonify({"status": "ok"})
    status = 503 if "unavailable" in msg.lower() or "no active" in msg.lower() else 502
    return jsonify({"error": msg}), status


@app.route("/api/control/seek", methods=["POST"])
def control_seek():
    """Seek within the current track (kiosk twist gesture)."""
    data = request.get_json(silent=True) or {}
    try:
        position_ms = max(0, int(data.get("position_ms")))
    except (TypeError, ValueError):
        return jsonify({"error": "position_ms required"}), 400

    try:
        resp = requests.post(
            f"{GO_LIBRESPOT_API_BASE}/player/seek",
            json={"position": position_ms},
            timeout=2.5,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"Local player API unavailable: {e}"}), 503
    if resp.status_code in (200, 204):
        return jsonify({"status": "ok", "position_ms": position_ms})
    return jsonify({"error": f"Local player API error: {resp.status_code}"}), 502


@app.route("/api/control/volume", methods=["POST"])
def control_volume():
    """Set playback volume by percent (kiosk fader/pinch gestures)."""
    data = request.get_json(silent=True) or {}
    try:
        percent = max(0, min(100, int(data.get("percent"))))
    except (TypeError, ValueError):
        return jsonify({"error": "percent required"}), 400

    # Translate percent to go-librespot volume steps.
    steps_max = 100
    try:
        status = requests.get(f"{GO_LIBRESPOT_API_BASE}/status", timeout=1.5)
        if status.status_code == 200:
            steps_max = int(status.json().get("volume_steps") or 100)
    except (requests.RequestException, ValueError):
        pass

    try:
        resp = requests.post(
            f"{GO_LIBRESPOT_API_BASE}/player/volume",
            json={"volume": int(round(percent / 100 * steps_max))},
            timeout=2.5,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"Local player API unavailable: {e}"}), 503
    if resp.status_code in (200, 204):
        return jsonify({"status": "ok", "percent": percent})
    return jsonify({"error": f"Local player API error: {resp.status_code}"}), 502


@app.route("/api/idle/playlists")
def idle_playlists():
    """Return house playlists for the idle launcher."""
    payload = idle_launcher_payload()
    return jsonify({
        "playlists": payload["playlists"],
        "title": payload["title"],
        "join_url": f"{get_public_base_url()}/join",
    })


@app.route("/api/crate")
def crate():
    """Sections of browsable music for the kiosk crate UI."""
    return jsonify(crate_payload())


@app.route("/api/idle/play", methods=["POST"])
def idle_play():
    """Start playback from a crate / idle launcher card."""
    data = request.get_json(silent=True) or {}
    uri = data.get("uri", "")
    allowed = {item["uri"] for item in idle_launcher_payload()["playlists"]}
    for section in crate_payload()["sections"]:
        allowed.update(item["uri"] for item in section["items"])
    if uri not in allowed:
        return jsonify({"error": "Playlist is not configured for this display"}), 400

    ok, msg = play_uri_local(uri)
    if ok:
        return jsonify({"status": "ok"})
    status = 503 if "unavailable" in msg.lower() or "not ready" in msg.lower() else 502
    return jsonify({"error": msg}), status


@app.route("/api/info")
def info():
    """Return server info including the LAN URL."""
    ip = get_local_ip()
    return jsonify({"ip": ip, "port": 5000, "url": f"http://{ip}:5000"})


# ── WLED discovery + setup ───────────────────────────────────


def _wled_known_ips():
    with _wled_devices_lock:
        return list(_wled_devices.keys())


def _wled_record_device(name, ip, port, pixel_count=None):
    if not ip:
        return
    with _wled_devices_lock:
        _wled_devices[ip] = {
            "name": name or ip,
            "ip": ip,
            "port": port or 80,
            "pixel_count": pixel_count,
            "last_seen": time.time(),
        }


def _wled_active_devices():
    cutoff = time.time() - WLED_DEVICE_TTL_SECONDS
    with _wled_devices_lock:
        # Evict stale entries on read.
        stale = [ip for ip, info in _wled_devices.items() if info["last_seen"] < cutoff]
        for ip in stale:
            _wled_devices.pop(ip, None)
        return [
            {
                "name": info["name"],
                "ip": info["ip"],
                "port": info["port"],
                "pixel_count": info.get("pixel_count"),
            }
            for info in _wled_devices.values()
        ]


WLED_SCAN_INTERVAL_SECONDS = 30
WLED_PROBE_TIMEOUT = 0.4
WLED_PROBE_CONCURRENCY = 32


def _is_wled_info(info):
    if not isinstance(info, dict):
        return False
    if info.get("brand") == "WLED":
        return True
    # Older firmware does not set "brand"; fall back to a structural check
    # that's unique to WLED: ESP arch + WLED's realtime UDP port + LED config.
    return (
        info.get("arch") in ("esp32", "esp8266")
        and isinstance(info.get("udpport"), int)
        and isinstance(info.get("leds"), dict)
    )


def _probe_wled(ip):
    try:
        resp = requests.get(f"http://{ip}/json/info", timeout=WLED_PROBE_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        info = resp.json()
    except ValueError:
        return None
    if not _is_wled_info(info):
        return None
    leds = info.get("leds") or {}
    pixel_count = int(leds.get("count") or 0) or None
    return (info.get("name") or ip, ip, 80, pixel_count)


def _start_wled_lan_scanner():
    """Periodically probe the local /24 for WLED devices.

    Replaces an earlier mDNS-based browser that bound to UDP 5353 and
    conflicted with avahi-daemon. go-librespot uses avahi for Spotify
    Connect advertisement; the dual-bind triggered a spam of
    "failed handling zeroconf add user request" and intermittent
    receiver dropouts. An HTTP scan touches no shared sockets and
    catches every WLED that's reachable on the LAN.
    """

    def _run():
        # Discover the local /24 from this host's LAN IP. Assumes a typical
        # home subnet — fine for the kind of LAN this kiosk lives on.
        local = get_local_ip()
        try:
            network = ipaddress.ip_network(f"{local}/24", strict=False)
            hosts = [str(h) for h in network.hosts() if str(h) != local]
        except ValueError:
            print(f"WLED scan: could not derive /24 from local IP {local}")
            return

        print(f"WLED scan: watching {network} every {WLED_SCAN_INTERVAL_SECONDS}s")

        while True:
            try:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=WLED_PROBE_CONCURRENCY
                ) as ex:
                    for result in ex.map(_probe_wled, hosts):
                        if result:
                            name, ip, port, pixel_count = result
                            _wled_record_device(name, ip, port, pixel_count)
            except Exception as e:
                print(f"WLED scan error: {e}")
            time.sleep(WLED_SCAN_INTERVAL_SECONDS)

    t = threading.Thread(target=_run, name="wled-lan-scan", daemon=True)
    t.start()


@app.route("/api/wled/discovered")
def wled_discovered():
    """Return WLED devices currently visible on the LAN."""
    return jsonify({"devices": _wled_active_devices()})


def _wled_config_devices(wled):
    """Same shape as wled_sync._normalize_devices — kept here to avoid a
    cross-module import. Returns a list of {host, name, pixel_count}."""
    raw = wled.get("devices")
    out = []
    if isinstance(raw, list) and raw:
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            host = (entry.get("host") or "").strip()
            if not host:
                continue
            out.append({
                "host": host,
                "name": (entry.get("name") or host).strip(),
                "pixel_count": max(1, int(entry.get("pixel_count") or 46)),
            })
        return out
    legacy_host = (wled.get("host") or "").strip()
    if legacy_host:
        out.append({
            "host": legacy_host,
            "name": (wled.get("name") or legacy_host).strip(),
            "pixel_count": max(1, int(wled.get("pixel_count") or 46)),
        })
    return out


@app.route("/api/wled/status")
def wled_status():
    """Return the kiosk-facing WLED configuration state."""
    config = load_config()
    wled = config.get("wled") or {}
    devices = _wled_config_devices(wled)
    return jsonify({
        "enabled": bool(wled.get("enabled", False)),
        "devices": devices,
        "configured": bool(devices),
    })


@app.route("/api/wled/devices", methods=["POST"])
def wled_devices_update():
    """Atomically replace the configured WLED device list.

    Body: {"devices": [{"host": "...", "name": "...", "pixel_count": 46}], "enabled": bool}
    Sending {"devices": []} clears the list (and effectively releases WLED on
    the next wled_sync tick); `enabled` is optional and defaults to True when
    devices is non-empty.
    """
    data = request.get_json(silent=True) or {}
    incoming = data.get("devices")
    if not isinstance(incoming, list):
        return jsonify({"error": "devices must be a list"}), 400

    devices = []
    for entry in incoming:
        if not isinstance(entry, dict):
            continue
        host = (entry.get("host") or "").strip()
        if not host:
            continue
        devices.append({
            "host": host,
            "name": (entry.get("name") or host).strip(),
            "pixel_count": max(1, int(entry.get("pixel_count") or 46)),
        })

    config = load_config()
    wled = config.get("wled") or {}
    wled["devices"] = devices
    # Clean up legacy single-device keys once we've written the new shape.
    for legacy_key in ("host", "name", "pixel_count"):
        wled.pop(legacy_key, None)
    if "enabled" in data:
        wled["enabled"] = bool(data["enabled"])
    elif devices and not wled.get("enabled"):
        wled["enabled"] = True
    config["wled"] = wled
    save_config(config)
    return "", 204


@app.route("/api/lyrics")
def lyrics():
    """Fetch synced lyrics from LRCLIB for a given track."""
    track_name = request.args.get("track", "")
    artist_name = request.args.get("artist", "")
    album_name = request.args.get("album", "")
    duration = request.args.get("duration", "0")
    if not track_name or not artist_name:
        return jsonify({"error": "Missing track/artist"}), 400
    try:
        resp = requests.get("https://lrclib.net/api/get", params={
            "track_name": track_name,
            "artist_name": artist_name,
            "album_name": album_name,
            "duration": duration,
        }, headers={"User-Agent": "SpotifyPiDisplay/2.0"}, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            synced = data.get("syncedLyrics") or ""
            plain = data.get("plainLyrics") or ""
            return jsonify({"syncedLyrics": synced, "plainLyrics": plain})
        return jsonify({"syncedLyrics": "", "plainLyrics": ""}), 200
    except Exception:
        return jsonify({"syncedLyrics": "", "plainLyrics": ""}), 200


if __name__ == "__main__":
    _start_wled_lan_scanner()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
