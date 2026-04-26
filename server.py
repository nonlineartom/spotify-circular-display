#!/usr/bin/env python3
"""Flask server — Spotify Connect display with QR-based user takeover.

Display: Raspotify's --onevent writes playback state to /tmp/spotify-state.json.
Track metadata is enriched via Spotify client credentials (no user login needed).

Controls: Users scan the QR code → OAuth login → their refresh token is stored.
The Pi's touch controls (skip/pause) use that token via Spotify Web API.
When a new user scans, their token replaces the previous one.
"""

import json
import os
import socket
import time
import urllib.parse
import requests
from flask import Flask, request, render_template, jsonify, redirect

app = Flask(__name__)
app.secret_key = os.urandom(24)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
STATE_FILE = "/tmp/spotify-state.json"

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SCOPES = "user-modify-playback-state user-read-playback-state"

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


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


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
            "artists": [{"name": a.get("name", "")} for a in data.get("artists", [])],
            "album": {
                "name": data.get("album", {}).get("name", ""),
                "images": data.get("album", {}).get("images", []),
            },
        }
        _track_cache[track_id] = track_info
        return track_info

    except Exception as e:
        print(f"Track lookup failed for {track_id}: {e}")
        return None


def read_playback_state():
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


def control_playback(action):
    """Control playback via Spotify Web API (requires user token)."""
    token = get_user_token()
    if not token:
        return False, "No user token — visit /login to authorize controls"

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


# ── UI routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/connect")
def connect():
    """Mobile-friendly page explaining how to connect to Pi Display."""
    return render_template("connect.html")


@app.route("/login")
def login():
    """One-time OAuth to enable playback controls (skip/pause)."""
    config = load_config()
    client_id = config.get("client_id", "")
    # Build redirect URI from request
    redirect_uri = request.url_root.rstrip("/") + "/callback"
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
    })
    return redirect(f"{SPOTIFY_AUTH_URL}?{params}")


@app.route("/callback")
def callback():
    """OAuth callback — stores refresh token for playback controls."""
    code = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        return f"Authorization failed: {error or 'no code'}", 400

    config = load_config()
    redirect_uri = request.url_root.rstrip("/") + "/callback"

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
    """Return current playback state from local Raspotify events."""
    state = read_playback_state()
    if state is None:
        return "", 204  # No content — nothing playing
    return jsonify(state)


@app.route("/api/health")
def health():
    """Return raw local playback/event health for troubleshooting."""
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except FileNotFoundError:
        return jsonify({"ok": False, "reason": "state_file_missing", "path": STATE_FILE}), 503
    except json.JSONDecodeError:
        return jsonify({"ok": False, "reason": "state_file_invalid", "path": STATE_FILE}), 503

    timestamp = state.get("timestamp") or 0
    age = max(0, time.time() - timestamp) if timestamp else None
    return jsonify({
        "ok": True,
        "path": STATE_FILE,
        "event": state.get("event", ""),
        "track_id": state.get("track_id"),
        "is_playing": bool(state.get("is_playing", False)),
        "position_ms": state.get("position_ms"),
        "duration_ms": state.get("duration_ms"),
        "volume_percent": state.get("volume_percent"),
        "age_seconds": None if age is None else round(age, 1),
    })


@app.route("/api/control/<action>", methods=["POST"])
def control(action):
    """Control playback via Spotify Web API (next, previous, play-pause)."""
    if action not in ("next", "previous", "play-pause"):
        return jsonify({"error": "Invalid action"}), 400
    ok, msg = control_playback(action)
    if ok:
        return jsonify({"status": "ok"})
    return jsonify({"error": msg}), 500


@app.route("/api/qr")
def qr_matrix():
    """Generate a QR code matrix (2D boolean array) for the given text."""
    text = request.args.get("text", "")
    if not text:
        return jsonify({"error": "Missing text parameter"}), 400
    try:
        import qrcode
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=1,
            border=0,
        )
        qr.add_data(text)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        return jsonify([[bool(cell) for cell in row] for row in matrix])
    except ImportError:
        return jsonify({"error": "qrcode library not installed"}), 500


@app.route("/api/info")
def info():
    """Return server info including local IP for QR code generation."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"
    return jsonify({"ip": ip, "port": 5000, "url": f"http://{ip}:5000"})


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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
