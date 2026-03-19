#!/usr/bin/env python3
"""Flask server — Spotify auth, API proxy, and web UI for Pi Display."""

import json
import os
import time
import requests
from flask import Flask, redirect, request, render_template, jsonify, session

app = Flask(__name__)
app.secret_key = os.urandom(24)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-read-currently-playing "
    "streaming"
)


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_token():
    """Return a valid access token, refreshing if needed."""
    config = load_config()
    if config.get("access_token") and config.get("token_expiry", 0) > time.time() + 60:
        return config["access_token"]
    # Refresh
    if not config.get("refresh_token"):
        return None
    resp = requests.post(SPOTIFY_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": config["refresh_token"],
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
    })
    if resp.status_code != 200:
        return None
    data = resp.json()
    config["access_token"] = data["access_token"]
    config["token_expiry"] = time.time() + data["expires_in"]
    if "refresh_token" in data:
        config["refresh_token"] = data["refresh_token"]
    save_config(config)
    return config["access_token"]


def spotify_request(method, endpoint, **kwargs):
    """Make an authenticated request to the Spotify API."""
    token = get_token()
    if not token:
        return jsonify({"error": "Not authenticated"}), 401
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{SPOTIFY_API_BASE}{endpoint}"
    resp = requests.request(method, url, headers=headers, **kwargs)
    if resp.status_code == 204:
        return "", 204
    try:
        return jsonify(resp.json()), resp.status_code
    except Exception:
        return "", resp.status_code


# ── Auth routes ──────────────────────────────────────────────

@app.route("/login")
def login():
    config = load_config()
    params = {
        "client_id": config["client_id"],
        "response_type": "code",
        "redirect_uri": config["redirect_uri"],
        "scope": SCOPES,
    }
    url = SPOTIFY_AUTH_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return redirect(url)


@app.route("/callback")
def callback():
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"Auth error: {error}", 400
    config = load_config()
    resp = requests.post(SPOTIFY_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config["redirect_uri"],
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
    })
    if resp.status_code != 200:
        return f"Token error: {resp.text}", 400
    data = resp.json()
    config["access_token"] = data["access_token"]
    config["refresh_token"] = data["refresh_token"]
    config["token_expiry"] = time.time() + data["expires_in"]
    save_config(config)
    return "<h1>Authenticated! You can close this tab.</h1>"


# ── UI route ─────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Playback API proxy ───────────────────────────────────────

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
    return spotify_request("PUT", f"/me/player/shuffle?state={state}")


@app.route("/api/repeat", methods=["PUT"])
def repeat():
    state = request.args.get("state", "off")
    return spotify_request("PUT", f"/me/player/repeat?state={state}")


@app.route("/api/seek", methods=["PUT"])
def seek():
    position_ms = request.args.get("position_ms", 0)
    return spotify_request("PUT", f"/me/player/seek?position_ms={position_ms}")


@app.route("/api/volume", methods=["PUT"])
def volume():
    volume_percent = request.args.get("volume_percent", 50)
    return spotify_request("PUT", f"/me/player/volume?volume_percent={volume_percent}")


@app.route("/api/devices")
def devices():
    return spotify_request("GET", "/me/player/devices")


@app.route("/api/transfer", methods=["PUT"])
def transfer():
    data = request.get_json()
    return spotify_request("PUT", "/me/player", json=data)


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
            synced = data.get("syncedLyrics") or ""
            plain = data.get("plainLyrics") or ""
            return jsonify({"syncedLyrics": synced, "plainLyrics": plain})
        return jsonify({"syncedLyrics": "", "plainLyrics": ""}), 200
    except Exception:
        return jsonify({"syncedLyrics": "", "plainLyrics": ""}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
