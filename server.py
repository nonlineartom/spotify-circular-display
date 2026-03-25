#!/usr/bin/env python3
"""Flask server — Spotify auth, API proxy, and web UI for Pi Display."""

import json
import logging
import os
import time
from collections import Counter
from io import BytesIO

import requests
from flask import Flask, redirect, request, render_template, jsonify
from PIL import Image

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("spotify-display")

# ── App setup ────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.urandom(24)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-read-currently-playing "
    "streaming"
)

# ── Settings ─────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "screensaver_timeout_minutes": 10,
    "lyrics_enabled": True,
    "spin_speed_rpm": 33.333,
    "volume_step": 5,
    "screensaver_enabled": True,
}

SETTINGS_VALIDATORS = {
    "screensaver_timeout_minutes": lambda v: isinstance(v, (int, float)) and v > 0,
    "lyrics_enabled": lambda v: isinstance(v, bool),
    "spin_speed_rpm": lambda v: isinstance(v, (int, float)) and v > 0,
    "volume_step": lambda v: isinstance(v, int) and 1 <= v <= 100,
    "screensaver_enabled": lambda v: isinstance(v, bool),
}


def load_settings():
    """Load settings from disk, creating the file with defaults if missing."""
    if not os.path.exists(SETTINGS_FILE):
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)
        # Backfill any missing keys with defaults
        merged = {**DEFAULT_SETTINGS, **settings}
        return merged
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read settings file, using defaults: %s", exc)
        return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    """Persist settings to disk."""
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


# ── Config helpers ───────────────────────────────────────────

def load_config():
    """Load Spotify credentials / tokens from config.json."""
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load config.json: %s", exc)
        return {}


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# ── Token management ────────────────────────────────────────

def get_token():
    """Return a valid access token, refreshing if needed."""
    config = load_config()
    if config.get("access_token") and config.get("token_expiry", 0) > time.time() + 60:
        return config["access_token"]

    # Refresh
    refresh_token = config.get("refresh_token")
    if not refresh_token:
        logger.warning("No refresh token available — user must log in")
        return None

    try:
        resp = requests.post(SPOTIFY_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.get("client_id", ""),
            "client_secret": config.get("client_secret", ""),
        }, timeout=10)
    except requests.RequestException as exc:
        logger.error("Token refresh request failed: %s", exc)
        return None

    if resp.status_code != 200:
        logger.error("Token refresh returned %d: %s", resp.status_code, resp.text)
        return None

    data = resp.json()
    config["access_token"] = data["access_token"]
    config["token_expiry"] = time.time() + data["expires_in"]
    if "refresh_token" in data:
        config["refresh_token"] = data["refresh_token"]
    save_config(config)
    logger.info("Access token refreshed successfully")
    return config["access_token"]


# ── Spotify API proxy ───────────────────────────────────────

def spotify_request(method, endpoint, params=None, **kwargs):
    """Make an authenticated request to the Spotify API.

    Returns a Flask response tuple.
    """
    token = get_token()
    if not token:
        logger.warning("Unauthenticated API call to %s — redirecting to /login", endpoint)
        return jsonify({"error": "Not authenticated. Please visit /login."}), 401

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{SPOTIFY_API_BASE}{endpoint}"

    try:
        resp = requests.request(
            method, url, headers=headers, params=params, timeout=10, **kwargs,
        )
    except requests.RequestException as exc:
        logger.error("Spotify API request failed (%s %s): %s", method, endpoint, exc)
        return jsonify({"error": "Spotify API request failed"}), 502

    if resp.status_code == 204:
        return "", 204

    try:
        body = resp.json()
    except ValueError:
        logger.warning(
            "Non-JSON response from Spotify (%s %s, status %d)",
            method, endpoint, resp.status_code,
        )
        return "", resp.status_code

    if resp.status_code >= 400:
        logger.warning(
            "Spotify API error (%s %s): %d — %s",
            method, endpoint, resp.status_code, body,
        )

    return jsonify(body), resp.status_code


# ── Auth routes ──────────────────────────────────────────────

@app.route("/login")
def login():
    config = load_config()
    if not config.get("client_id") or not config.get("redirect_uri"):
        logger.error("Missing client_id or redirect_uri in config.json")
        return "Server misconfigured — check config.json", 500

    auth_params = {
        "client_id": config["client_id"],
        "response_type": "code",
        "redirect_uri": config["redirect_uri"],
        "scope": SCOPES,
    }
    auth_url = requests.Request("GET", SPOTIFY_AUTH_URL, params=auth_params).prepare().url
    return redirect(auth_url)


@app.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        logger.warning("OAuth callback error: %s", error)
        return f"Auth error: {error}", 400

    code = request.args.get("code")
    if not code:
        return "Missing authorization code", 400

    config = load_config()
    try:
        resp = requests.post(SPOTIFY_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.get("redirect_uri", ""),
            "client_id": config.get("client_id", ""),
            "client_secret": config.get("client_secret", ""),
        }, timeout=10)
    except requests.RequestException as exc:
        logger.error("Token exchange request failed: %s", exc)
        return "Token exchange failed — check server logs", 500

    if resp.status_code != 200:
        logger.error("Token exchange returned %d: %s", resp.status_code, resp.text)
        return "Token exchange failed — check server logs", 400

    data = resp.json()
    config["access_token"] = data["access_token"]
    config["refresh_token"] = data["refresh_token"]
    config["token_expiry"] = time.time() + data["expires_in"]
    save_config(config)
    logger.info("User authenticated successfully")
    return "<h1>Authenticated! You can close this tab.</h1>"


# ── UI routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


# ── Settings API ─────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["PUT"])
def update_settings():
    body = request.get_json(silent=True)
    if not body or not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    current = load_settings()
    errors = {}

    for key, value in body.items():
        if key not in DEFAULT_SETTINGS:
            errors[key] = "Unknown setting"
            continue
        validator = SETTINGS_VALIDATORS[key]
        if not validator(value):
            errors[key] = f"Invalid value: {value!r}"
            continue
        current[key] = value

    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 400

    save_settings(current)
    logger.info("Settings updated: %s", list(body.keys()))
    return jsonify(current)


# ── Playback API proxy ──────────────────────────────────────

@app.route("/api/now-playing")
def now_playing():
    return spotify_request("GET", "/me/player")


@app.route("/api/play", methods=["PUT"])
def play():
    return spotify_request("PUT", "/me/player/play")


@app.route("/api/pause", methods=["PUT"])
def pause():
    return spotify_request("PUT", "/me/player/pause")


@app.route("/api/next", methods=["POST"])
def next_track():
    return spotify_request("POST", "/me/player/next")


@app.route("/api/previous", methods=["POST"])
def previous_track():
    return spotify_request("POST", "/me/player/previous")


@app.route("/api/shuffle", methods=["PUT"])
def shuffle():
    state = request.args.get("state", "true")
    if state not in ("true", "false"):
        return jsonify({"error": "state must be 'true' or 'false'"}), 400
    return spotify_request("PUT", "/me/player/shuffle", params={"state": state})


@app.route("/api/repeat", methods=["PUT"])
def repeat():
    state = request.args.get("state", "off")
    if state not in ("off", "track", "context"):
        return jsonify({"error": "state must be 'off', 'track', or 'context'"}), 400
    return spotify_request("PUT", "/me/player/repeat", params={"state": state})


@app.route("/api/seek", methods=["PUT"])
def seek():
    raw = request.args.get("position_ms", "0")
    try:
        position_ms = int(raw)
    except (ValueError, TypeError):
        return jsonify({"error": "position_ms must be a non-negative integer"}), 400
    if position_ms < 0:
        return jsonify({"error": "position_ms must be a non-negative integer"}), 400
    return spotify_request("PUT", "/me/player/seek", params={"position_ms": position_ms})


@app.route("/api/volume", methods=["PUT"])
def volume():
    raw = request.args.get("volume_percent", "50")
    try:
        volume_percent = int(raw)
    except (ValueError, TypeError):
        return jsonify({"error": "volume_percent must be an integer between 0 and 100"}), 400
    if not 0 <= volume_percent <= 100:
        return jsonify({"error": "volume_percent must be an integer between 0 and 100"}), 400
    return spotify_request("PUT", "/me/player/volume", params={"volume_percent": volume_percent})


@app.route("/api/devices")
def devices():
    return spotify_request("GET", "/me/player/devices")


@app.route("/api/transfer", methods=["PUT"])
def transfer():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400
    return spotify_request("PUT", "/me/player", json=data)


@app.route("/api/queue")
def queue():
    return spotify_request("GET", "/me/player/queue")


# ── Lyrics ───────────────────────────────────────────────────

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
        }, headers={"User-Agent": "SpotifyPiDisplay/1.0"}, timeout=5)

        if resp.status_code == 200:
            data = resp.json()
            return jsonify({
                "syncedLyrics": data.get("syncedLyrics") or "",
                "plainLyrics": data.get("plainLyrics") or "",
            })

        logger.info("LRCLIB returned %d for '%s' by '%s'", resp.status_code, track_name, artist_name)
        return jsonify({"syncedLyrics": "", "plainLyrics": ""})

    except requests.RequestException as exc:
        logger.warning("LRCLIB request failed: %s", exc)
        return jsonify({"syncedLyrics": "", "plainLyrics": ""})


# ── Color extraction ─────────────────────────────────────────

@app.route("/api/colors")
def colors():
    """Fetch an image and extract dominant and accent colors."""
    image_url = request.args.get("image_url", "")
    if not image_url:
        return jsonify({"error": "Missing image_url query parameter"}), 400

    try:
        resp = requests.get(image_url, timeout=5)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to fetch image for color extraction: %s", exc)
        return jsonify({"error": "Could not fetch image"}), 502

    try:
        img = Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception as exc:
        logger.warning("Failed to open image: %s", exc)
        return jsonify({"error": "Invalid image data"}), 400

    # Resize to a small thumbnail for fast processing
    img = img.resize((50, 50), Image.LANCZOS)
    pixels = list(img.getdata())

    # Filter out near-black and near-white pixels
    filtered = [
        p for p in pixels
        if not (p[0] < 20 and p[1] < 20 and p[2] < 20)
        and not (p[0] > 235 and p[1] > 235 and p[2] > 235)
    ]

    if not filtered:
        # Fall back to all pixels if everything was filtered
        filtered = pixels

    # Quantise to reduce noise: round each channel to nearest 16
    quantised = [
        (r // 16 * 16, g // 16 * 16, b // 16 * 16) for r, g, b in filtered
    ]

    counts = Counter(quantised)
    most_common = counts.most_common(2)

    dominant = list(most_common[0][0]) if most_common else [0, 0, 0]
    accent = list(most_common[1][0]) if len(most_common) > 1 else dominant

    return jsonify({"dominant": dominant, "accent": accent})


# ── Main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    # Ensure settings file exists on startup
    load_settings()
    app.run(host="0.0.0.0", port=5000, debug=False)
