#!/usr/bin/env python3
"""Flask server — Spotify Connect display.

Display: go-librespot's local API is preferred for playback state and controls.
Raspotify's --onevent state file remains as a fallback for older installs.
Track metadata can also be enriched via Spotify client credentials.

Controls: the Pi's touch controls call the local Spotify Connect receiver API.
The legacy Spotify Web API OAuth path is retained only as a fallback.
"""

import base64
import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import socket
import stat
import threading
import time
import urllib.parse
from collections import OrderedDict, deque
from functools import wraps
from tempfile import NamedTemporaryFile

import requests
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
)

from backlight import BacklightController
from spotify_profiles import (
    AliasCollisionError,
    ProfileLimitError,
    normalize_alias,
    normalize_identifier,
    normalize_profile,
    normalize_store as normalize_profile_store,
    profile_for_alias,
    public_profile,
    reauthorization_deadline,
    remove_profile,
    upsert_profile,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_REQUEST_BYTES", 64 * 1024))
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("SPOTIFY_DISPLAY_CONFIG", os.path.join(BASE_DIR, "config.json"))
IDLE_PLAYLISTS_FILE = os.path.join(BASE_DIR, "idle_playlists.json")
RECENT_SPINS_FILE = os.path.join(BASE_DIR, "recently_spun.json")
IDLE_PLAYLISTS_EXAMPLE_FILE = os.path.join(BASE_DIR, "idle_playlists.example.json")
STATE_FILE = os.environ.get("SPOTIFY_STATE_FILE", "/run/spotify-display/spotify-state.json")
LEGACY_STATE_FILE = "/tmp/spotify-state.json"
WLED_STATUS_FILE = os.environ.get("WLED_STATUS_FILE", "/run/spotify-display/wled-status.json")
GO_LIBRESPOT_API_BASE = os.environ.get("GO_LIBRESPOT_API_BASE", "http://127.0.0.1:3678").rstrip("/")
SERVER_PORT = int(os.environ.get("DISPLAY_PORT") or os.environ.get("PORT", "5000"))

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
LIBRARY_SCOPES = (
    "user-read-private",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
    "user-top-read",
)
PLAYBACK_SCOPES = ("user-read-playback-state", "user-modify-playback-state")
# Backwards-compatible names for operators importing this module. OAuth uses
# ``_oauth_scopes`` so playback authority remains opt-in.
SCOPES = " ".join(PLAYBACK_SCOPES)
PLAYLIST_SCOPES = " ".join(LIBRARY_SCOPES)

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
# Per-account access tokens. The three scalar names remain diagnostic mirrors
# for older local tooling; authorization decisions never read them.
_user_tokens = {}
_user_token = None
_user_token_expiry = 0
_user_token_grant_id = None
class BoundedTTLCache:
    """Small thread-safe LRU+TTL cache used for remote metadata.

    The display runs for months at a time, so plain dictionaries keyed by every
    track/album ever seen are not safe.  This intentionally implements only the
    mapping operations used in this module.
    """

    def __init__(self, maxsize, ttl):
        self.maxsize = maxsize
        self.ttl = ttl
        self._items = OrderedDict()
        self._lock = threading.RLock()

    def _purge(self):
        now = time.time()
        expired = [key for key, (deadline, _) in self._items.items() if deadline <= now]
        for key in expired:
            self._items.pop(key, None)

    def set(self, key, value, ttl=None):
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = (time.time() + (self.ttl if ttl is None else ttl), value)
            if len(self._items) > self.maxsize:
                self._purge()
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def get(self, key, default=None):
        with self._lock:
            item = self._items.pop(key, None)
            if item is None:
                return default
            if item[0] <= time.time():
                return default
            self._items[key] = item
            return item[1]

    def __contains__(self, key):
        marker = object()
        return self.get(key, marker) is not marker

    def __getitem__(self, key):
        marker = object()
        value = self.get(key, marker)
        if value is marker:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        self.set(key, value)

    def clear(self):
        with self._lock:
            self._items.clear()

    def pop(self, key, default=None):
        with self._lock:
            item = self._items.pop(key, None)
            return default if item is None or item[0] <= time.time() else item[1]

    def __len__(self):
        with self._lock:
            self._purge()
            return len(self._items)


_track_cache = BoundedTTLCache(2048, 12 * 60 * 60)
_playlist_cache = {"loaded_at": 0, "items": []}
_album_cache = BoundedTTLCache(1024, 12 * 60 * 60)
_album_tracks_cache = BoundedTTLCache(512, 12 * 60 * 60)
_uri_image_cache = BoundedTTLCache(1024, 24 * 60 * 60)
_uri_image_failed = BoundedTTLCache(1024, 10 * 60)
_artist_albums_cache = BoundedTTLCache(512, 6 * 60 * 60)
_recent_spins = {"loaded": False, "items": []}  # newest first
_recent_spins_lock = threading.Lock()
_last_spin_album = None
_crate_cache = {"built_at": 0, "payload": None}  # generic/legacy test alias
_crate_caches = {"generic": _crate_cache}
_crate_build_lock = threading.RLock()
_crate_build_condition = threading.Condition(_crate_build_lock)
_crate_building = False
_crate_building_key = None
_account_generation = 0
_profile_generations = {}
_enrich_inflight = set()
_enrich_last_attempt = BoundedTTLCache(2048, 10 * 60)
_enrich_lock = threading.Lock()

_config_lock = threading.RLock()
_client_token_lock = threading.Lock()
_user_token_lock = threading.RLock()

_lyrics_cache = BoundedTTLCache(512, 24 * 60 * 60)
_lyrics_failure_times = deque(maxlen=10)
_lyrics_breaker_until = 0.0
_lyrics_lock = threading.Lock()
_lyrics_inflight = set()
_pairing_tokens = BoundedTTLCache(32, 10 * 60)
_kiosk_pairing = {
    "url": None,
    "digest": None,
    "expires_at": 0,
    "profile_epoch": None,
    "profile_kind": None,
}
_pairing_lock = threading.Lock()

# Receiver identities are opaque, process-local observations. Epochs are
# deliberately random rather than sequential so a browser flow from a prior
# service process cannot accidentally bind after restart.
_receiver_identity_lock = threading.RLock()
_receiver_identity = {"alias": None, "epoch": secrets.token_urlsafe(18), "active": False}
_legacy_migration_attempts = BoundedTTLCache(32, 5 * 60)
_profile_prune_lock = threading.Lock()
_profile_prune_next = 0.0

_started_at = time.time()
_background_lock = threading.Lock()
_background_started = False
_background_components_started = set()
_backlight_lock = threading.Lock()
_backlight_controller = None
_event_condition = threading.Condition()
_event_monitor_started = False
_event_version = 0
_event_signal = {
    "version": 0,
    "active": False,
    "track_id": None,
    "is_playing": False,
    "profile_state": "no_receiver",
    "profile_epoch": _receiver_identity["epoch"],
    "receiver_available": False,
}
_event_clients = 0
MAX_SSE_CLIENTS = max(1, min(int(os.environ.get("MAX_SSE_CLIENTS", "2")), 8))

# WLED discovery cache: ip -> {"name": str, "ip": str, "port": int, "last_seen": float}
_wled_devices = {}
_wled_devices_lock = threading.Lock()
WLED_DEVICE_TTL_SECONDS = 120
_wled_scan_condition = threading.Condition()
_wled_scan_state = {
    "demand_until": 0.0,
    "last_started": float("-inf"),
    "running": False,
    "worker_started": False,
}
MAX_CONFIG_BYTES = 1024 * 1024
MAX_RUNTIME_STATE_BYTES = 64 * 1024


class ConfigWriteRefused(RuntimeError):
    """Raised when a write would overwrite an existing unreadable config."""

    def __init__(self, state):
        self.state = state
        super().__init__(f"Configuration is {state}; refusing to replace it")


def _atomic_write_json(path, payload, mode=0o600):
    """Durably replace a JSON file without exposing a partially-written file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = None
    try:
        with NamedTemporaryFile("w", dir=directory, prefix=".tmp-", delete=False) as tmp:
            temp_path = tmp.name
            json.dump(payload, tmp, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        temp_path = None
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems (notably network shares) cannot fsync directories.
            pass
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def _read_config_file():
    """Return (mapping, status) without ever modifying the source bytes."""
    try:
        with open(CONFIG_FILE, "rb") as f:
            raw = f.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return {}, {"ok": True, "state": "missing", "writable": True}
    except OSError:
        return None, {"ok": False, "state": "unreadable", "writable": False}
    if len(raw) > MAX_CONFIG_BYTES:
        return None, {"ok": False, "state": "too_large", "writable": False}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, {"ok": False, "state": "malformed", "writable": False}
    if not isinstance(data, dict):
        return None, {"ok": False, "state": "wrong_type", "writable": False}
    return data, {"ok": True, "state": "valid", "writable": True}


def config_status():
    with _config_lock:
        _config, status = _read_config_file()
        return dict(status)


def _normalize_config(config):
    """Return a type-safe copy while preserving unknown forward-compatible keys."""
    normalized = dict(config) if isinstance(config, dict) else {}

    for section_name in ("security", "spotify_session", "wled", "backlight"):
        if section_name not in normalized:
            continue
        section = normalized[section_name]
        normalized[section_name] = dict(section) if isinstance(section, dict) else {}

    for key in (
        "client_id",
        "client_secret",
        "refresh_token",
        "public_base_url",
        "redirect_uri",
        "owner_token",
        "legacy_web_api_device_id",
    ):
        if key in normalized and not isinstance(normalized[key], str):
            normalized[key] = ""

    security = normalized.get("security", {})
    for key in ("session_secret", "owner_token"):
        if key in security and not isinstance(security[key], str):
            security[key] = ""

    wled = normalized.get("wled", {})
    if "devices" in wled and not isinstance(wled["devices"], list):
        wled["devices"] = []
    for key in ("host", "name"):
        if key in wled and not isinstance(wled[key], str):
            wled[key] = ""
    if "enabled" in wled and not isinstance(wled["enabled"], bool):
        wled["enabled"] = False

    spotify_session = normalized.get("spotify_session", {})
    if "kind" in spotify_session and spotify_session["kind"] not in (
        "guest", "owner", "household"
    ):
        spotify_session["kind"] = "invalid"
    for key in ("connected_at", "expires_at", "authorized_at", "reauthorize_at"):
        value = spotify_session.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            spotify_session[key] = None

    if (
        "allow_web_api_control_fallback" in normalized
        and not isinstance(normalized["allow_web_api_control_fallback"], bool)
    ):
        normalized["allow_web_api_control_fallback"] = False
    guest_hours = normalized.get("guest_session_hours")
    if guest_hours is not None and (
        isinstance(guest_hours, bool)
        or not isinstance(guest_hours, (int, float))
        or not math.isfinite(float(guest_hours))
    ):
        normalized["guest_session_hours"] = 12
    if "spotify_profiles" in normalized:
        normalized["spotify_profiles"] = normalize_profile_store(normalized["spotify_profiles"])
    return normalized


def load_config():
    with _config_lock:
        config, _status = _read_config_file()
        return _normalize_config(config) if config is not None else {}


def save_config(config):
    if not isinstance(config, dict):
        raise TypeError("config must be a dictionary")
    with _config_lock:
        _current, status = _read_config_file()
        if not status["writable"]:
            raise ConfigWriteRefused(status["state"])
        _atomic_write_json(CONFIG_FILE, _normalize_config(config), mode=0o600)


def update_config(mutator):
    """Locked read-modify-write transaction; returns the mutator's result."""
    with _config_lock:
        config, status = _read_config_file()
        if not status["writable"]:
            raise ConfigWriteRefused(status["state"])
        config = _normalize_config(config)
        result = mutator(config)
        _atomic_write_json(CONFIG_FILE, config, mode=0o600)
        return result


def _profile_store(config=None):
    config = load_config() if config is None else config
    return normalize_profile_store(config.get("spotify_profiles"))


def _profile_by_id(account_id, config=None):
    account_id = normalize_identifier(account_id)
    if not account_id:
        return None
    profile = _profile_store(config)["profiles"].get(account_id)
    return dict(profile) if profile else None


def _oauth_scopes(config=None):
    config = load_config() if config is None else config
    scopes = list(LIBRARY_SCOPES)
    if bool(config.get("allow_web_api_control_fallback", False)):
        scopes.extend(PLAYBACK_SCOPES)
    return scopes


def _grant_is_expired(profile, now=None):
    """Return whether a retained grant has reached its local cutoff.

    New household grants have no local expiry, but version-1 owner/household
    records could carry one that the previous server enforced. Preserve that
    stricter migration boundary instead of silently widening access.
    """
    profile = normalize_profile(profile)
    if not profile:
        return True
    now = time.time() if now is None else now
    expires_at = profile.get("expires_at")
    return expires_at is not None and now >= expires_at


def _grant_reauthorization_due(profile, now=None):
    """Return whether Spotify's six-calendar-month authorization is due.

    Version-1 profiles have no trustworthy issuance timestamp. Their deadline
    remains unknown until the next explicit authorization; provider
    ``invalid_grant`` still fails closed and removes only that account.
    """
    profile = normalize_profile(profile)
    if not profile:
        return True
    now = time.time() if now is None else now
    reauthorize_at = profile.get("reauthorize_at")
    return reauthorize_at is not None and now >= reauthorize_at


def _last_household_profile(store):
    """Most recently authenticated household grant, if any.

    Owner preference: while the receiver has no session at all, the idle shelf
    keeps the last household user's crates indefinitely instead of reverting
    to House picks. Guest grants never persist this way, and an active but
    unknown listener still gets House picks only.
    """
    best = None
    for profile in store["profiles"].values():
        if profile.get("kind") != "household":
            continue
        seen = max(
            profile.get("connected_at") or 0,
            profile.get("authorized_at") or 0,
        )
        if best is None or seen > best[1]:
            best = (profile, seen)
    return dict(best[0]) if best else None


def _legacy_grant(config=None):
    """Return the pre-profile grant without ever binding it implicitly."""
    config = load_config() if config is None else config
    refresh_token = config.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return None
    grant = config.get("spotify_session")
    grant = grant if isinstance(grant, dict) else {}
    kind = (
        grant.get("kind")
        if grant.get("kind") in ("guest", "owner", "household")
        else "owner"
    )
    expires_at = grant.get("expires_at")
    if kind == "guest" and not isinstance(expires_at, (int, float)):
        return None
    return {
        "refresh_token": refresh_token,
        "kind": kind,
        "connected_at": grant.get("connected_at") if isinstance(grant.get("connected_at"), (int, float)) else 0,
        "expires_at": expires_at if isinstance(expires_at, (int, float)) else None,
        "authorized_at": grant.get("authorized_at")
        if isinstance(grant.get("authorized_at"), (int, float)) else None,
        "reauthorize_at": grant.get("reauthorize_at")
        if isinstance(grant.get("reauthorize_at"), (int, float)) else None,
    }


def _get_backlight_controller():
    """Construct the HID controller lazily, without writing during import/tests."""
    global _backlight_controller
    if _backlight_controller is not None:
        return _backlight_controller
    with _backlight_lock:
        if _backlight_controller is None:
            config = load_config()
            section = config.get("backlight") if isinstance(config.get("backlight"), dict) else {}

            def number(name, default, whole=False):
                try:
                    value = float(section.get(name, default))
                    if not math.isfinite(value):
                        raise ValueError
                except (TypeError, ValueError, OverflowError):
                    value = float(default)
                return int(round(value)) if whole else value

            enabled = section.get("enabled", True)
            if not isinstance(enabled, bool):
                enabled = str(enabled).strip().lower() in ("1", "true", "yes", "on")
            safe_section = {
                "enabled": enabled,
                "safe_max_percent": number("safe_max_percent", 80),
                "initial_percent": number("initial_percent", 100, whole=True),
                "idle_percent": number("idle_percent", 10, whole=True),
                "ramp_interval_ms": number("ramp_interval_ms", 150),
                "retry_interval_seconds": number("retry_interval_seconds", 2),
            }
            _backlight_controller = BacklightController.from_application_config({"backlight": safe_section})
        return _backlight_controller


def _configure_session_secret():
    configured = os.environ.get("FLASK_SECRET_KEY")
    if configured:
        app.secret_key = configured
        return

    with _config_lock:
        config, status = _read_config_file()
    if not status["writable"]:
        # Keep serving enough diagnostics to repair the file, but never turn
        # malformed credentials into a fresh partial config on import.
        app.secret_key = secrets.token_urlsafe(48)
        print(f"Configuration is {status['state']}; using an ephemeral session secret")
        return
    config = _normalize_config(config)
    security = config.get("security") if isinstance(config.get("security"), dict) else {}
    configured = security.get("session_secret")
    if not configured:
        configured = secrets.token_urlsafe(48)

        def store_secret(latest):
            latest_security = latest.get("security")
            if not isinstance(latest_security, dict):
                latest_security = {}
            # Another thread/process may have populated it since the first read.
            latest_security.setdefault("session_secret", configured)
            latest["security"] = latest_security

        update_config(store_secret)
        configured = (load_config().get("security") or {}).get("session_secret", configured)
    app.secret_key = configured


def _configure_cookie_security():
    explicit = os.environ.get("SESSION_COOKIE_SECURE")
    if explicit is not None:
        app.config["SESSION_COOKIE_SECURE"] = explicit.lower() in ("1", "true", "yes", "on")
        return
    config = load_config()
    public_url = os.environ.get("PUBLIC_BASE_URL") or config.get("public_base_url") or ""
    # A redirect URI alone says nothing about the origin serving the session
    # cookie. Only the canonical public base (or an explicit override) may
    # enable Secure cookies.
    app.config["SESSION_COOKIE_SECURE"] = str(public_url).lower().startswith("https://")


_configure_session_secret()
_configure_cookie_security()


def resolve_uri_image(uri):
    """Best-effort cover art for a spotify:{type}:{id} URI via client credentials.

    Lets idle_playlists.json entries omit the manual "image" field — the kiosk
    shelf still gets real sleeves. Successes cache for 24 hours; failures back
    off for 10 minutes.
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
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {"playlists": []}
    if not isinstance(data, dict):
        data = {"playlists": []}
    raw_playlists = data.get("playlists")
    if not isinstance(raw_playlists, list):
        raw_playlists = []

    playlists = []
    for idx, item in enumerate(raw_playlists):
        if not isinstance(item, dict):
            continue
        uri = item.get("uri", "")
        if not isinstance(uri, str) or not uri.startswith("spotify:"):
            continue
        image = item.get("image", "")
        if not isinstance(image, str):
            image = ""
        playlists.append({
            "id": f"house-{idx}",
            "title": item.get("title") if isinstance(item.get("title"), str) else "Playlist",
            "subtitle": item.get("subtitle") if isinstance(item.get("subtitle"), str) else "House pick",
            "uri": uri,
            "image": image or resolve_uri_image(uri),
            "accent": item.get("accent") if isinstance(item.get("accent"), str) else "#ffffff",
        })

    _playlist_cache = {"loaded_at": time.time(), "items": playlists}
    return playlists


def get_client_token():
    """Get a Spotify app-level token via client credentials flow.

    This does NOT require a user to log in — only the app's
    client_id and client_secret are needed.
    """
    global _client_token, _client_token_expiry

    with _client_token_lock:
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
                print(f"Client credentials error: {resp.status_code}")
                return None
            data = resp.json()
            if not isinstance(data, dict):
                return None
            access_token = data.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                return None
            _client_token = access_token
            _client_token_expiry = time.time() + _token_lifetime(data.get("expires_in"))
            return _client_token
        except (requests.RequestException, KeyError, TypeError, ValueError) as e:
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
                "total_tracks": data.get("album", {}).get("total_tracks", 0),
            },
        }
        _track_cache[track_id] = track_info
        return track_info

    except Exception as e:
        print(f"Track lookup failed for {track_id}: {e}")
        return None


def lookup_album(album_id):
    """Look up album-level metadata (record label) via client credentials.

    Successful results use a bounded 12-hour cache; failures retry after 90s.
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
            _album_cache.set(album_id, {}, ttl=90)
            return None
        info = {"label": resp.json().get("label", "")}
        _album_cache[album_id] = info
        return info
    except Exception as e:
        print(f"Album lookup failed for {album_id}: {e}")
        return None


def lookup_album_tracks(album_id):
    """Full tracklist for an album via client credentials (no user login).

    Returns a list of {number, disc, name, duration_ms, uri}, ordered as on the
    record. Successful results use a bounded 12-hour cache; transient failures
    return [] without caching so a later request can retry.
    """
    if not album_id:
        return []
    if album_id in _album_tracks_cache:
        return _album_tracks_cache[album_id]

    token = get_client_token()
    if not token:
        return []

    tracks = []
    url = f"{SPOTIFY_API_BASE}/albums/{album_id}/tracks"
    offset = 0
    try:
        # Use our own fixed-origin offsets rather than following the upstream
        # `next` URL with an Authorization header. This keeps the bearer token
        # on api.spotify.com and bounds even a malformed pagination loop.
        for _page in range(10):
            resp = requests.get(
                url,
                params={"limit": 50, "offset": offset},
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            if resp.status_code != 200:
                print(f"Album tracks error for {album_id}: {resp.status_code}")
                return []
            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                return []
            page_items = data["items"]
            for t in page_items:
                if not isinstance(t, dict):
                    continue
                uri = t.get("uri") if isinstance(t.get("uri"), str) else ""
                if not uri.startswith("spotify:track:"):
                    continue
                tracks.append({
                    "number": t.get("track_number", len(tracks) + 1),
                    "disc": t.get("disc_number", 1),
                    "name": t.get("name", ""),
                    "duration_ms": t.get("duration_ms", 0),
                    "uri": uri,
                })
            next_url = data.get("next")
            if not isinstance(next_url, str) or not next_url or len(page_items) < 50:
                break
            offset += len(page_items)
    except (requests.RequestException, TypeError, ValueError) as e:
        print(f"Album tracks fetch failed for {album_id}: {e}")
        return []

    _album_tracks_cache[album_id] = tracks
    return tracks


def current_album_id():
    """Album id of whatever is playing right now, from the enrichment cache.

    None until the current track's first enrichment lands. Used to scope the
    tracklist + play-track endpoints to the record actually on the platter.
    """
    state = read_playback_state()
    item = (state or {}).get("item") or {}
    direct = (item.get("album") or {}).get("id")
    if direct:
        return direct
    track_id = item.get("id")
    cached = _track_cache.get(track_id) if track_id else None
    return ((cached or {}).get("album") or {}).get("id")


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
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        raw_items = data.get("items") if isinstance(data, dict) else []
        if not isinstance(raw_items, list):
            raw_items = []
        items = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            uri = entry.get("uri")
            if not isinstance(uri, str) or not uri.startswith("spotify:"):
                continue
            safe = dict(entry)
            safe["uri"] = uri
            artist_ids = safe.get("artist_ids")
            safe["artist_ids"] = (
                [artist_id for artist_id in artist_ids if isinstance(artist_id, str) and artist_id]
                if isinstance(artist_ids, list) else []
            )
            items.append(safe)
        _recent_spins["items"] = items[:40]
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
            _atomic_write_json(RECENT_SPINS_FILE, {"items": _recent_spins["items"]})
        except OSError as e:
            print(f"Could not persist recent spins: {e}")


def recent_spin_items():
    _load_recent_spins()
    with _recent_spins_lock:
        return [dict(e) for e in _recent_spins["items"]]


def _response_object_items(response):
    """Return only object entries from an upstream `{items: [...]}` payload."""
    try:
        payload = response.json()
    except ValueError:
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return []
    return [entry for entry in payload["items"] if isinstance(entry, dict)]


def _first_image_url(images):
    if not isinstance(images, list) or not images or not isinstance(images[0], dict):
        return ""
    url = images[0].get("url")
    return url if isinstance(url, str) else ""


def _token_lifetime(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        seconds = int(float(value))
    except (TypeError, ValueError, OverflowError):
        seconds = 3600
    return max(60, min(86400, seconds))


def fetch_artist_albums(artist_id, fallback_artist_name=""):
    """Albums by an artist via client credentials — fuels the 'Deeper cuts'
    crate. Stored in a bounded six-hour cache per artist."""
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
            _artist_albums_cache.set(artist_id, [], ttl=90)
            return []
        items = []
        for a in _response_object_items(resp):
            images = a.get("images") if isinstance(a.get("images"), list) else []
            artists = a.get("artists") if isinstance(a.get("artists"), list) else []
            artist = ", ".join(
                x.get("name", "") for x in artists
                if isinstance(x, dict) and isinstance(x.get("name"), str)
            ) or fallback_artist_name
            uri = a.get("uri") if isinstance(a.get("uri"), str) else ""
            if not uri.startswith("spotify:album:"):
                continue
            items.append({
                "id": f"deep-{a.get('id', '')}",
                "uri": uri,
                "title": a.get("name") if isinstance(a.get("name"), str) else "Album",
                "subtitle": artist,
                "image": _first_image_url(images),
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

    # Warm uncached artists in parallel — sequentially this was up to six
    # back-to-back API calls and the main source of slow crate builds.
    uncached = [a for a in artist_ids[:6] if a not in _artist_albums_cache]
    if uncached:
        threads = [threading.Thread(target=fetch_artist_albums, args=(aid,)) for aid in uncached]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=6)

    items = []
    for aid in artist_ids[:6]:
        for album in _artist_albums_cache.get(aid) or []:
            if album["uri"] in seen_albums:
                continue
            seen_albums.add(album["uri"])
            items.append(album)
    return items[:24]


def _build_crate_payload(account_id=None, profile_epoch=None):
    """Build a profile-isolated crate, or a non-private generic fallback."""
    if account_id and not _profile_epoch_matches(account_id, profile_epoch):
        return None
    results = {}

    def grab(key, fn):
        try:
            results[key] = fn()
        except Exception as e:
            print(f"Crate section '{key}' failed: {e}")
            results[key] = []

    sources = [("house", load_idle_playlists)]
    if account_id:
        sources[:0] = [
            ("yours", lambda: fetch_user_playlists(
                limit=50, account_id=account_id, profile_epoch=profile_epoch
            )),
            ("saved", lambda: fetch_saved_albums(
                limit=50, account_id=account_id, profile_epoch=profile_epoch
            )),
            ("rotation", lambda: fetch_top_albums(
                limit=50, account_id=account_id, profile_epoch=profile_epoch
            )),
        ]
    threads = [threading.Thread(target=grab, args=(key, fn)) for key, fn in sources]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=12)

    sections = []
    if results.get("yours"):
        sections.append({"id": "yours", "title": "Your playlists", "items": results["yours"]})
    if results.get("saved"):
        sections.append({"id": "saved", "title": "Your albums", "items": results["saved"]})
    if results.get("rotation"):
        sections.append({"id": "rotation", "title": "Your rotation", "items": results["rotation"]})
    if results.get("house"):
        sections.append({"id": "house", "title": "House picks", "items": results["house"]})
    return {"sections": sections}


def _crate_cache_for(cache_key):
    with _crate_build_lock:
        cache = _crate_caches.get(cache_key)
        if cache is None:
            cache = {"built_at": 0, "payload": None}
            _crate_caches[cache_key] = cache
        return cache


def _crate_generation(context):
    account_id = context.get("account_id")
    return _profile_generations.get(account_id, 0) if account_id else _account_generation


def _decorate_crate_payload(payload, context):
    result = dict(payload or {"sections": []})
    result.update(_public_profile_context(context, include_name=True))
    return result


def _crate_context_is_current(context):
    current = _receiver_context()
    return bool(
        hmac.compare_digest(str(current["profile_epoch"]), str(context["profile_epoch"]))
        and current["cache_key"] == context["cache_key"]
        and current["profile_state"] == context["profile_state"]
    )


def _safe_crate_payload(payload, context):
    if not _crate_context_is_current(context):
        return _decorate_crate_payload(
            {"sections": [], "building": True}, _receiver_context()
        )
    return _decorate_crate_payload(payload, context)


def _rebuild_crate_async(context=None):
    """Refresh one profile cache on a background thread, at most one at a time."""
    global _crate_building, _crate_building_key
    context = _maybe_migrate_legacy_profile(context or _receiver_context())
    cache_key = context["cache_key"]
    account_id = context.get("account_id")
    with _crate_build_lock:
        if _crate_building:
            return
        _crate_building = True
        _crate_building_key = cache_key
        build_generation = _crate_generation(context)

    def job():
        global _crate_building, _crate_building_key
        payload = None
        try:
            payload = _build_crate_payload(account_id, context["profile_epoch"])
        finally:
            with _crate_build_condition:
                if (
                    payload is not None
                    and build_generation == _crate_generation(context)
                    and _crate_context_is_current(context)
                ):
                    cache = _crate_cache_for(cache_key)
                    cache["payload"] = payload
                    cache["built_at"] = time.time()
                _crate_building = False
                _crate_building_key = None
                _crate_build_condition.notify_all()

    threading.Thread(target=job, daemon=True).start()


def crate_payload(context=None):
    """All browsable music, in sections, for the kiosk crate UI.

    Stale-while-revalidate: a cached payload is always served immediately —
    if it has gone stale, a background rebuild refreshes it for the next
    request. Only a completely cold cache builds synchronously.
    """
    context = _maybe_migrate_legacy_profile(context or _receiver_context())
    cache_key = context["cache_key"]
    account_id = context.get("account_id")
    cache = _crate_cache_for(cache_key)
    now = time.time()
    with _crate_build_lock:
        cached = cache["payload"]
        cached_at = cache["built_at"]
    if cached and now - cached_at < 120:
        return _safe_crate_payload(cached, context)
    if cached:
        _rebuild_crate_async(context)
        return _safe_crate_payload(cached, context)

    global _crate_building, _crate_building_key
    # Do not launch a second cold build while the startup warmer is already
    # fetching the same four Spotify sections.  Wait briefly, then return a
    # valid "building" payload rather than blocking a Flask worker for 12s.
    with _crate_build_condition:
        if _crate_building:
            _crate_build_condition.wait(timeout=3.0)
            if cache["payload"]:
                return _safe_crate_payload(cache["payload"], context)
            return _safe_crate_payload({"sections": [], "building": True}, context)
        # Another cold request can finish after our optimistic cache read but
        # before we claim the global build slot. Re-read while holding the
        # condition lock so we never build again from stale state.
        if cache["payload"]:
            return _safe_crate_payload(cache["payload"], context)
        _crate_building = True
        _crate_building_key = cache_key
        build_generation = _crate_generation(context)

    result = None
    try:
        payload = _build_crate_payload(account_id, context["profile_epoch"])
        with _crate_build_lock:
            if (
                payload is not None
                and build_generation == _crate_generation(context)
                and _crate_context_is_current(context)
            ):
                cache["payload"] = payload
                cache["built_at"] = time.time()
                result = _safe_crate_payload(payload, context)
            else:
                # Never return or cache library data from the disconnected or
                # replaced account after its generation has been invalidated.
                result = _decorate_crate_payload(
                    {"sections": [], "building": True},
                    _receiver_context(),
                )
    finally:
        with _crate_build_condition:
            _crate_building = False
            _crate_building_key = None
            _crate_build_condition.notify_all()
    return result


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
    for key in ("album_type", "release_date", "total_tracks"):
        if cached_album.get(key) and not album.get(key):
            album[key] = cached_album[key]

    album_id = cached_album.get("id")
    if album_id:
        album.setdefault("id", album_id)  # lets the kiosk fetch the album's tracklist
        extra = _album_cache.get(album_id)
        if extra is None:
            queue_track_enrichment(track_id)
        elif extra.get("label") and not album.get("label"):
            album["label"] = extra["label"]


def fetch_user_playlists(limit=6, account_id=None, profile_epoch=None):
    """Fetch playlists for the currently authorized Spotify user, if present."""
    if account_id is not None and not _profile_epoch_matches(account_id, profile_epoch):
        return []
    token = (
        get_user_token()
        if account_id is None
        else get_user_token(account_id, profile_epoch=profile_epoch)
    )
    if not token:
        return []
    if account_id is not None and not _profile_epoch_matches(account_id, profile_epoch):
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
    for idx, item in enumerate(_response_object_items(resp)):
        uri = item.get("uri") if isinstance(item.get("uri"), str) else ""
        if not uri.startswith("spotify:playlist:"):
            continue
        owner_data = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        owner = owner_data.get("display_name") if isinstance(owner_data.get("display_name"), str) else ""
        images = item.get("images") if isinstance(item.get("images"), list) else []
        playlists.append({
            "id": f"user-{idx}",
            "title": item.get("name") if isinstance(item.get("name"), str) else "Playlist",
            "subtitle": owner or "Your playlist",
            "uri": uri,
            "image": _first_image_url(images),
            "accent": "#1db954",
            "source": "user",
        })
    return playlists


def fetch_saved_albums(limit=50, account_id=None, profile_epoch=None):
    """The user's saved Spotify albums — needs the user-library-read scope.

    Returns [] quietly until the OAuth token has been re-granted with that
    scope (403 before then), so the crate simply omits the section.
    """
    if account_id is not None and not _profile_epoch_matches(account_id, profile_epoch):
        return []
    token = (
        get_user_token()
        if account_id is None
        else get_user_token(account_id, profile_epoch=profile_epoch)
    )
    if not token:
        return []
    if account_id is not None and not _profile_epoch_matches(account_id, profile_epoch):
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
    for idx, entry in enumerate(_response_object_items(resp)):
        album = entry.get("album") if isinstance(entry.get("album"), dict) else {}
        uri = album.get("uri") if isinstance(album.get("uri"), str) else ""
        if not uri.startswith("spotify:album:"):
            continue
        images = album.get("images") if isinstance(album.get("images"), list) else []
        artists = album.get("artists") if isinstance(album.get("artists"), list) else []
        artist = ", ".join(
            a.get("name", "") for a in artists
            if isinstance(a, dict) and isinstance(a.get("name"), str)
        )
        albums.append({
            "id": f"saved-{idx}",
            "title": album.get("name") if isinstance(album.get("name"), str) else "Album",
            "subtitle": artist,
            "uri": uri,
            "image": _first_image_url(images),
            "accent": "#4cb8a4",
            "type": "album",
        })
    return albums


def fetch_top_albums(limit=50, account_id=None, profile_epoch=None):
    """Deduplicated albums from one profile's medium-term top tracks."""
    if account_id is not None and not _profile_epoch_matches(account_id, profile_epoch):
        return []
    token = (
        get_user_token()
        if account_id is None
        else get_user_token(account_id, profile_epoch=profile_epoch)
    )
    if not token:
        return []
    if account_id is not None and not _profile_epoch_matches(account_id, profile_epoch):
        return []
    try:
        response = requests.get(
            f"{SPOTIFY_API_BASE}/me/top/tracks",
            params={"limit": min(50, limit), "offset": 0, "time_range": "medium_term"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
    except requests.RequestException as error:
        print(f"Top tracks lookup failed: {error}")
        return []
    if response.status_code != 200:
        if response.status_code != 403:
            print(f"Top tracks lookup error: {response.status_code}")
        return []

    albums = []
    seen = set()
    for entry in _response_object_items(response):
        album = entry.get("album") if isinstance(entry.get("album"), dict) else {}
        uri = album.get("uri") if isinstance(album.get("uri"), str) else ""
        if not uri.startswith("spotify:album:") or uri in seen:
            continue
        seen.add(uri)
        images = album.get("images") if isinstance(album.get("images"), list) else []
        artists = album.get("artists") if isinstance(album.get("artists"), list) else []
        artist = ", ".join(
            item.get("name", "") for item in artists
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        albums.append({
            "id": f"rotation-{album.get('id') or len(albums)}",
            "title": album.get("name") if isinstance(album.get("name"), str) else "Album",
            "subtitle": artist,
            "uri": uri,
            "image": _first_image_url(images),
            "accent": "#c47bd8",
            "type": "album",
        })
        if len(albums) >= 24:
            break
    return albums


def idle_launcher_payload(include_private=True, account_id=None, profile_epoch=None):
    user_playlists = (
        fetch_user_playlists(account_id=account_id, profile_epoch=profile_epoch)
        if include_private and account_id else []
    )
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


def _observe_receiver_identity(alias, active=True):
    """Record a trusted go-librespot identity without exposing it to clients."""
    alias = normalize_alias(alias) if active else None
    active = bool(active and alias)
    with _receiver_identity_lock:
        if (
            _receiver_identity["active"] != active
            or _receiver_identity["alias"] != alias
        ):
            _receiver_identity.update({
                "alias": alias,
                "active": active,
                "epoch": secrets.token_urlsafe(18),
            })
        return dict(_receiver_identity)


def _receiver_identity_snapshot():
    with _receiver_identity_lock:
        return dict(_receiver_identity)


def _bump_receiver_epoch(expected_epoch=None):
    """Invalidate browser/profile work after a grant mapping changes."""
    with _receiver_identity_lock:
        if expected_epoch is not None and not hmac.compare_digest(
            str(expected_epoch), str(_receiver_identity["epoch"])
        ):
            return None
        _receiver_identity["epoch"] = secrets.token_urlsafe(18)
        return dict(_receiver_identity)


def _receiver_context(config=None):
    if config is None:
        config = load_config()
    identity = _receiver_identity_snapshot()
    base = {
        "profile_state": "no_receiver",
        "profile_epoch": identity["epoch"],
        "profile_name": None,
        "reauth_required": False,
        "account_id": None,
        "receiver_alias": None,
        "cache_key": "generic",
        "expired_account_id": None,
        "reauthorize_account_id": None,
    }
    if identity["active"] and identity["alias"]:
        base["profile_state"] = "unlinked"
        base["receiver_alias"] = identity["alias"]
        profile = profile_for_alias(_profile_store(config), identity["alias"])
    else:
        profile = _last_household_profile(_profile_store(config))
    if not profile:
        return base
    if _grant_is_expired(profile):
        base["expired_account_id"] = profile["account_id"]
        return base
    if _grant_reauthorization_due(profile):
        # Keep the durable alias mapping so a correct reauthorization replaces
        # the same household profile. Private access remains unavailable until
        # then, which makes stale tokens/caches fail closed.
        base["profile_name"] = profile.get("display_name") or None
        base["profile_state"] = "reauth_required"
        base["reauth_required"] = True
        base["reauthorize_account_id"] = profile["account_id"]
        return base
    base.update({
        "profile_state": "linked",
        "profile_name": profile.get("display_name") or None,
        "account_id": profile["account_id"],
        "cache_key": f"profile:{profile['account_id']}",
    })
    return base


def _public_profile_context(context=None, include_name=False):
    context = _receiver_context() if context is None else context
    payload = {
        "profile_state": context["profile_state"],
        "profile_epoch": context["profile_epoch"],
    }
    if include_name:
        payload["profile_name"] = context.get("profile_name")
    return payload


def _profile_epoch_matches(account_id, profile_epoch):
    account_id = normalize_identifier(account_id)
    if not account_id or not isinstance(profile_epoch, str):
        return False
    context = _receiver_context()
    return bool(
        context.get("account_id") == account_id
        and hmac.compare_digest(str(context.get("profile_epoch")), profile_epoch)
    )


def _receiver_binding_matches(binding, refresh=False):
    if not isinstance(binding, dict):
        return False
    available = True
    if refresh:
        available, _state = read_go_librespot_state()
    current = _receiver_identity_snapshot()
    alias = normalize_alias(binding.get("receiver_alias"))
    epoch = binding.get("profile_epoch")
    return bool(
        available
        and current["active"]
        and isinstance(epoch, str)
        and hmac.compare_digest(str(current["epoch"]), epoch)
        and (alias is None or current["alias"] == alias)
    )


def _profile_changed_response(context=None):
    context = _receiver_context() if context is None else context
    return jsonify({
        "error": "The active Spotify profile changed; refresh the crate",
        "code": "profile_changed",
        **_public_profile_context(context),
    }), 409


def _read_legacy_state_file():
    """Read the runtime state, falling back to the old /tmp path for upgrades."""
    candidates = [STATE_FILE]
    if STATE_FILE != LEGACY_STATE_FILE:
        candidates.append(LEGACY_STATE_FILE)
    last_error = "state_file_missing"
    for path in candidates:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                file_stat = os.fstat(fd)
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_size <= 0
                    or file_stat.st_size > MAX_RUNTIME_STATE_BYTES
                ):
                    raise ValueError("runtime state has an invalid size/type")
                with os.fdopen(fd, "rb") as state_file:
                    fd = None
                    raw = state_file.read(MAX_RUNTIME_STATE_BYTES + 1)
                if len(raw) > MAX_RUNTIME_STATE_BYTES:
                    raise ValueError("runtime state is too large")
                data = json.loads(raw.decode("utf-8"))
            finally:
                if fd is not None:
                    os.close(fd)
            if isinstance(data, dict):
                return data, path, None
            last_error = "state_file_invalid"
        except FileNotFoundError:
            continue
        except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError):
            last_error = "state_file_invalid"
    return None, STATE_FILE, last_error


def read_go_librespot_state():
    """Read playback state from go-librespot's local API.

    Returns (available, state). If the API is reachable but there is no active
    session, available is True and state is None, preventing stale fallback data
    from an old Raspotify state file from showing on the display.
    """
    def unavailable():
        # Unknown receiver identity is not authorization. Rotate the epoch so
        # browsers and in-flight private fetches fail closed immediately.
        _observe_receiver_identity(None, active=False)
        return False, None

    try:
        resp = requests.get(f"{GO_LIBRESPOT_API_BASE}/status", timeout=0.8)
    except requests.RequestException:
        return unavailable()

    if resp.status_code == 204:
        _observe_receiver_identity(None, active=False)
        return True, None
    if resp.status_code != 200:
        print(f"go-librespot status error: {resp.status_code}")
        return unavailable()

    try:
        status = resp.json()
    except ValueError:
        return unavailable()
    if not isinstance(status, dict):
        return unavailable()

    username = status.get("username")
    if username is not None and normalize_alias(username) is None:
        return unavailable()

    for flag in ("stopped", "paused", "buffering"):
        if flag in status and not isinstance(status[flag], bool):
            return unavailable()
    if status.get("stopped") is True:
        _observe_receiver_identity(username, active=username is not None)
        return True, None

    track = status.get("track")
    if not isinstance(track, dict) or not track:
        return unavailable()

    def nonnegative_number(value, default=0):
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError
        return int(number)

    uri = track.get("uri", "")
    if not isinstance(uri, str):
        return unavailable()
    track_id = spotify_uri_id(uri)
    artist_names = track.get("artist_names", [])
    if not isinstance(artist_names, list) or any(not isinstance(name, str) for name in artist_names):
        return unavailable()
    artists = [{"name": name} for name in artist_names]
    cover_url = track.get("album_cover_url")
    if cover_url is not None and not isinstance(cover_url, str):
        return unavailable()
    images = [{"url": cover_url}] if cover_url else []
    for text_field in ("name", "album_name"):
        if text_field in track and not isinstance(track[text_field], str):
            return unavailable()
    for text_field in ("device_id", "device_name", "play_origin"):
        if text_field in status and status[text_field] is not None and not isinstance(status[text_field], str):
            return unavailable()

    try:
        duration = nonnegative_number(track.get("duration"))
        position = nonnegative_number(track.get("position"))
        volume_steps = nonnegative_number(status.get("volume_steps"), 100)
        volume = nonnegative_number(status.get("volume"), 0)
        if volume_steps <= 0:
            raise ValueError
        volume_percent = int(round((volume / max(volume_steps, 1)) * 100))
    except (TypeError, ValueError, OverflowError):
        return unavailable()

    _observe_receiver_identity(username, active=username is not None)

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
    state, _, _ = _read_legacy_state_file()
    if state is None:
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
    state, _available = read_playback_state_with_availability()
    return state


def read_playback_state_with_availability():
    """Return (state, source_available), distinguishing outage from true idle."""
    go_available, go_state = read_go_librespot_state()
    if go_available:
        return go_state, True
    _raw, _path, state_error = _read_legacy_state_file()
    if state_error:
        return None, False
    return read_raspotify_playback_state(), True


def _granted_scopes(value, fallback=()):
    scopes = value.split() if isinstance(value, str) else list(fallback)
    # Reuse profile normalisation to bound and validate scope strings.
    probe = normalize_profile({
        "account_id": "scope-probe",
        "refresh_token": "scope-probe",
        "kind": "owner",
        "connected_at": 0,
        "expires_at": None,
        "scopes": scopes,
    })
    return probe["scopes"] if probe else []


def _spotify_current_user(access_token):
    try:
        response = requests.get(
            f"{SPOTIFY_API_BASE}/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=8,
        )
    except requests.RequestException as error:
        print(f"Spotify profile lookup failed: {error}")
        return None
    if response.status_code != 200:
        print(f"Spotify profile lookup error: {response.status_code}")
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    account_id = normalize_identifier(data.get("account_id"))
    receiver_alias = normalize_alias(data.get("id"))
    if not account_id or not receiver_alias:
        return None
    display_name = data.get("display_name")
    return {
        "account_id": account_id,
        "receiver_alias": receiver_alias,
        "display_name": display_name if isinstance(display_name, str) else "",
    }


def _persist_profile_grant(
    profile, receiver_alias=None, clear_legacy=False, expected_legacy_token=None
):
    profile = normalize_profile(profile)
    if not profile:
        raise ValueError("invalid Spotify profile grant")

    pruned_account_ids = []

    def persist(latest):
        store = _profile_store(latest)
        for account_id, existing in list(store["profiles"].items()):
            if _grant_is_expired(existing):
                store = remove_profile(store, account_id)
                pruned_account_ids.append(account_id)
        latest["spotify_profiles"] = upsert_profile(store, profile, receiver_alias)
        if clear_legacy:
            current_legacy = latest.get("refresh_token")
            if expected_legacy_token is not None and not (
                isinstance(current_legacy, str)
                and hmac.compare_digest(current_legacy, str(expected_legacy_token))
            ):
                raise RuntimeError("legacy Spotify grant changed during migration")
            latest.pop("refresh_token", None)
            latest.pop("spotify_session", None)

    update_config(persist)
    for account_id in pruned_account_ids:
        _user_tokens.pop(account_id, None)
        _clear_user_caches(account_id)
    return profile


def _persist_bound_profile_grant(
    profile, receiver_alias, binding, clear_legacy=False, expected_legacy_token=None
):
    """Atomically validate receiver epoch and publish its alias mapping."""
    profile = normalize_profile(profile)
    receiver_alias = normalize_alias(receiver_alias)
    expected_epoch = binding.get("profile_epoch") if isinstance(binding, dict) else None
    if not profile or not receiver_alias or not isinstance(expected_epoch, str):
        return None
    pruned_account_ids = []
    new_epoch = None

    with _receiver_identity_lock:
        if not (
            _receiver_identity["active"]
            and _receiver_identity["alias"] == receiver_alias
            and hmac.compare_digest(str(_receiver_identity["epoch"]), expected_epoch)
        ):
            return None

        def persist(latest):
            store = _profile_store(latest)
            for account_id, existing in list(store["profiles"].items()):
                if _grant_is_expired(existing):
                    store = remove_profile(store, account_id)
                    pruned_account_ids.append(account_id)
            latest["spotify_profiles"] = upsert_profile(store, profile, receiver_alias)
            if clear_legacy:
                current_legacy = latest.get("refresh_token")
                if expected_legacy_token is not None and not (
                    isinstance(current_legacy, str)
                    and hmac.compare_digest(current_legacy, str(expected_legacy_token))
                ):
                    raise RuntimeError("legacy Spotify grant changed during migration")
                latest.pop("refresh_token", None)
                latest.pop("spotify_session", None)

        update_config(persist)
        new_epoch = secrets.token_urlsafe(18)
        _receiver_identity["epoch"] = new_epoch

    for account_id in pruned_account_ids:
        _user_tokens.pop(account_id, None)
        _clear_user_caches(account_id)
    return new_epoch


def _update_profile_grant(account_id, expected_refresh_token, rotated=None, scopes=None):
    updated = {"ok": False, "refresh_token": expected_refresh_token}

    def mutate(latest):
        store = _profile_store(latest)
        profile = store["profiles"].get(account_id)
        if not profile or not hmac.compare_digest(
            str(profile.get("refresh_token", "")), str(expected_refresh_token)
        ):
            return
        profile = dict(profile)
        if isinstance(rotated, str) and rotated:
            profile["refresh_token"] = rotated
            updated["refresh_token"] = rotated
        if scopes is not None:
            profile["scopes"] = list(scopes)
        latest["spotify_profiles"] = upsert_profile(store, profile)
        updated["ok"] = True

    update_config(mutate)
    return updated


def _remove_legacy_grant(expected_refresh_token=None):
    removed = {"ok": False}

    def clear(latest):
        current = latest.get("refresh_token")
        if expected_refresh_token is not None and not (
            isinstance(current, str)
            and hmac.compare_digest(current, str(expected_refresh_token))
        ):
            return
        if "refresh_token" in latest or "spotify_session" in latest:
            latest.pop("refresh_token", None)
            latest.pop("spotify_session", None)
            removed["ok"] = True

    update_config(clear)
    return removed["ok"]


def _rotate_legacy_grant(expected_refresh_token, rotated_refresh_token):
    if not isinstance(rotated_refresh_token, str) or not rotated_refresh_token:
        return False
    updated = {"ok": False}

    def rotate(latest):
        current = latest.get("refresh_token")
        if isinstance(current, str) and hmac.compare_digest(current, str(expected_refresh_token)):
            latest["refresh_token"] = rotated_refresh_token
            updated["ok"] = True

    update_config(rotate)
    return updated["ok"]


def _clear_user_caches(account_id=None):
    """Invalidate only one profile by default, or all profiles for maintenance."""
    global _playlist_cache, _account_generation
    with _crate_build_lock:
        _account_generation += 1
        if account_id:
            _profile_generations[account_id] = _profile_generations.get(account_id, 0) + 1
            _crate_caches.pop(f"profile:{account_id}", None)
        else:
            _profile_generations.clear()
            _crate_cache.update({"payload": None, "built_at": 0})
            _crate_caches.clear()
            _crate_caches["generic"] = _crate_cache
    if account_id is None:
        _playlist_cache = {"loaded_at": 0, "items": []}


def _prune_expired_profiles(config=None):
    """Remove all expired grants so dormant guests cannot retain capacity."""
    config = load_config() if config is None else config
    store = _profile_store(config)
    expired = {
        account_id: profile
        for account_id, profile in store["profiles"].items()
        if _grant_is_expired(profile)
    }
    legacy = _legacy_grant(config)
    legacy_expired = bool(
        legacy and _grant_is_expired({"account_id": "legacy", **legacy})
    )
    if not expired and not legacy_expired:
        return config

    removed = []
    removed_legacy = {"ok": False}

    def prune(latest):
        latest_store = _profile_store(latest)
        for account_id, profile in list(latest_store["profiles"].items()):
            if _grant_is_expired(profile):
                latest_store = remove_profile(latest_store, account_id)
                removed.append((account_id, list(profile.get("receiver_aliases", []))))
        latest["spotify_profiles"] = latest_store
        latest_legacy = _legacy_grant(latest)
        if latest_legacy and _grant_is_expired({"account_id": "legacy", **latest_legacy}):
            latest.pop("refresh_token", None)
            latest.pop("spotify_session", None)
            removed_legacy["ok"] = True

    update_config(prune)
    identity = _receiver_identity_snapshot()
    active_removed = False
    with _user_token_lock:
        for account_id, aliases in removed:
            _user_tokens.pop(account_id, None)
            _clear_user_caches(account_id)
            active_removed = active_removed or (
                identity["active"] and identity["alias"] in aliases
            )
    if active_removed:
        _bump_receiver_epoch(identity["epoch"])
    if removed_legacy["ok"]:
        _clear_user_caches()
    return load_config()


def _maybe_prune_expired_profiles(now=None, interval=60):
    """Bound pruning work at a request boundary with a single lock order."""
    global _profile_prune_next
    now = time.monotonic() if now is None else now
    with _profile_prune_lock:
        if now < _profile_prune_next:
            return
        _profile_prune_next = now + max(1, interval)
    _prune_expired_profiles()


def _remove_profile_grant(account_id, expected_refresh_token=None):
    account_id = normalize_identifier(account_id)
    if not account_id:
        return False
    removed = {"ok": False}

    def clear(latest):
        store = _profile_store(latest)
        profile = store["profiles"].get(account_id)
        if not profile:
            return
        if expected_refresh_token is not None and not hmac.compare_digest(
            str(profile.get("refresh_token", "")), str(expected_refresh_token)
        ):
            return
        latest["spotify_profiles"] = remove_profile(store, account_id)
        removed["ok"] = True

    update_config(clear)
    if removed["ok"]:
        _user_tokens.pop(account_id, None)
        _clear_user_caches(account_id)
    return removed["ok"]


def _disconnect_user_account(account_id=None, expected_refresh_token=None):
    """Forget one profile grant; unbound legacy credentials are separate."""
    global _user_token, _user_token_expiry, _user_token_grant_id
    with _user_token_lock:
        context = _receiver_context()
        account_id = normalize_identifier(
            account_id
            or context.get("account_id")
            or context.get("expired_account_id")
            or context.get("reauthorize_account_id")
        )
        if account_id:
            removed = _remove_profile_grant(account_id, expected_refresh_token)
            if removed and account_id in (
                context.get("account_id"),
                context.get("expired_account_id"),
                context.get("reauthorize_account_id"),
            ):
                _bump_receiver_epoch(context["profile_epoch"])
        else:
            removed = _remove_legacy_grant(expected_refresh_token)
            if removed:
                _clear_user_caches()
        _user_token = None
        _user_token_expiry = 0
        _user_token_grant_id = None
        return removed


def _refresh_token_response(refresh_token, config):
    try:
        response = requests.post(SPOTIFY_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, auth=(config.get("client_id", ""), config.get("client_secret", "")), timeout=5)
    except requests.RequestException as error:
        print(f"User token refresh failed: {error}")
        return None, False
    try:
        data = response.json()
    except ValueError:
        data = {}
    invalid_grant = (
        response.status_code == 400
        and isinstance(data, dict)
        and data.get("error") == "invalid_grant"
    )
    if response.status_code != 200:
        print(f"User token refresh error: {response.status_code}")
        return None, invalid_grant
    if not isinstance(data, dict):
        return None, False
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None, False
    return data, False


def _maybe_migrate_legacy_profile(context):
    """Migrate only when legacy `/me.id` exactly matches receiver username.

    A legacy grant has no receiver binding. Guessing would expose one account's
    library to another caster, so a mismatch remains quarantined until an
    explicit owner-approved OAuth pairing replaces it.
    """
    expired_account_id = context.get("expired_account_id")
    if expired_account_id:
        _disconnect_user_account(expired_account_id)
        context = _receiver_context()
    if context.get("reauth_required"):
        return context
    if context.get("profile_state") != "unlinked" or not context.get("receiver_alias"):
        return context
    config = load_config()
    legacy = _legacy_grant(config)
    if not legacy:
        return context
    if _grant_is_expired({"account_id": "legacy", **legacy}):
        _remove_legacy_grant(legacy["refresh_token"])
        return context
    marker = hashlib.sha256(
        f"{context['receiver_alias']}\0{legacy['refresh_token']}".encode()
    ).hexdigest()
    if _legacy_migration_attempts.get(marker):
        return context
    _legacy_migration_attempts.set(marker, True)

    with _user_token_lock:
        data, invalid_grant = _refresh_token_response(legacy["refresh_token"], config)
        if invalid_grant:
            _remove_legacy_grant(legacy["refresh_token"])
            return context
        if not data:
            return context
        rotated = data.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != legacy["refresh_token"]:
            if not _rotate_legacy_grant(legacy["refresh_token"], rotated):
                return context
            legacy["refresh_token"] = rotated
        identity = _spotify_current_user(data["access_token"])
        if not identity or identity["receiver_alias"] != context["receiver_alias"]:
            return context
        binding = {
            "receiver_alias": context["receiver_alias"],
            "profile_epoch": context["profile_epoch"],
        }
        if not _receiver_binding_matches(binding, refresh=True):
            return _receiver_context()
        refresh_token = legacy["refresh_token"]
        profile = {
            **identity,
            "refresh_token": refresh_token,
            "kind": legacy["kind"],
            "connected_at": legacy["connected_at"],
            "expires_at": legacy["expires_at"],
            # Pre-profile grants do not carry a trustworthy OAuth issuance
            # timestamp. Preserve the unknown lifecycle until provider
            # invalid_grant or the listener explicitly reauthorizes.
            "authorized_at": legacy.get("authorized_at"),
            "reauthorize_at": legacy.get("reauthorize_at"),
            "scopes": _granted_scopes(data.get("scope")),
            "receiver_aliases": [],
        }
        try:
            committed_epoch = _persist_bound_profile_grant(
                profile,
                context["receiver_alias"],
                binding,
                clear_legacy=True,
                expected_legacy_token=refresh_token,
            )
        except (AliasCollisionError, ValueError, RuntimeError):
            return context
        if not committed_epoch:
            return _receiver_context()
        expiry = time.time() + _token_lifetime(data.get("expires_in"))
        _user_tokens[identity["account_id"]] = {
            "access_token": data["access_token"],
            "expires_at": expiry,
            "grant_id": hashlib.sha256(refresh_token.encode()).hexdigest(),
        }
        return _receiver_context()


def get_user_token(account_id=None, profile_epoch=None):
    """Return a cached/refreshed token for one explicitly selected profile."""
    global _user_token, _user_token_expiry, _user_token_grant_id
    if account_id is None:
        configured_ids = set(_profile_store()["profiles"])
        with _user_token_lock:
            for stale_account_id in set(_user_tokens) - configured_ids:
                _user_tokens.pop(stale_account_id, None)
                _clear_user_caches(stale_account_id)
        legacy = _legacy_grant()
        if legacy and _grant_is_expired({"account_id": "legacy", **legacy}):
            _remove_legacy_grant(legacy["refresh_token"])
        context = _maybe_migrate_legacy_profile(_receiver_context())
        account_id = context.get("account_id")
        profile_epoch = context.get("profile_epoch")
    account_id = normalize_identifier(account_id)
    if not account_id:
        return None
    if not _profile_epoch_matches(account_id, profile_epoch):
        return None

    with _user_token_lock:
        config = load_config()
        profile = _profile_by_id(account_id, config)
        if not profile:
            _user_tokens.pop(account_id, None)
            _clear_user_caches(account_id)
            return None
        refresh_token = profile["refresh_token"]
        if _grant_is_expired(profile):
            _disconnect_user_account(account_id, refresh_token)
            return None
        if _grant_reauthorization_due(profile):
            _user_tokens.pop(account_id, None)
            return None
        client_id = config.get("client_id", "")
        client_secret = config.get("client_secret", "")
        if not client_id or not client_secret:
            return None

        grant_id = hashlib.sha256(refresh_token.encode()).hexdigest()
        cached = _user_tokens.get(account_id)
        if (
            cached
            and cached.get("grant_id") == grant_id
            and cached.get("expires_at", 0) > time.time() + 60
        ):
            return cached["access_token"] if _profile_epoch_matches(account_id, profile_epoch) else None

        if not _profile_epoch_matches(account_id, profile_epoch):
            return None
        data, invalid_grant = _refresh_token_response(refresh_token, config)
        if invalid_grant:
            _disconnect_user_account(account_id, refresh_token)
            return None
        if not data:
            return None
        rotated = data.get("refresh_token")
        rotated = rotated if isinstance(rotated, str) and rotated else None
        scopes = _granted_scopes(data.get("scope"), profile.get("scopes", []))
        update = _update_profile_grant(account_id, refresh_token, rotated, scopes)
        if not update["ok"]:
            return None
        refresh_token = update["refresh_token"]
        if not _profile_epoch_matches(account_id, profile_epoch):
            return None
        grant_id = hashlib.sha256(refresh_token.encode()).hexdigest()
        expiry = time.time() + _token_lifetime(data.get("expires_in"))
        cached = {
            "access_token": data["access_token"],
            "expires_at": expiry,
            "grant_id": grant_id,
        }
        _user_tokens[account_id] = cached
        _user_token = cached["access_token"]
        _user_token_expiry = expiry
        _user_token_grant_id = grant_id
        return cached["access_token"]


def get_local_ip():
    """Return the LAN IP reachable by phones on the same network."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def get_public_base_url():
    config = load_config()
    configured = os.environ.get("PUBLIC_BASE_URL") or config.get("public_base_url")
    if configured:
        return configured.rstrip("/")
    return f"http://{get_local_ip()}:{SERVER_PORT}"


class OAuthOriginError(RuntimeError):
    def __init__(self, message, expected_url=None, status_code=503):
        self.expected_url = expected_url
        self.status_code = status_code
        super().__init__(message)


class ReceiverIdentityError(RuntimeError):
    def __init__(self, message, code="receiver_unavailable", status_code=409):
        self.code = code
        self.status_code = status_code
        super().__init__(message)


def _url_origin(url):
    try:
        parsed = urllib.parse.urlsplit(str(url))
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme.lower(), parsed.hostname.lower(), port
    except (TypeError, ValueError):
        return None


def get_oauth_public_base_url(config=None):
    """Canonical externally-visible origin used by pairing and OAuth."""
    config = config or load_config()
    configured = os.environ.get("PUBLIC_BASE_URL") or config.get("public_base_url")
    if not configured:
        raise OAuthOriginError(
            "OAuth pairing requires PUBLIC_BASE_URL (or config public_base_url)"
        )
    parsed = urllib.parse.urlsplit(str(configured))
    if (
        _url_origin(configured) is None
        or parsed.path not in ("", "/")
        or "?" in str(configured)
        or "#" in str(configured)
        or parsed.query
        or parsed.fragment
    ):
        raise OAuthOriginError("public_base_url must be a bare http(s) origin")
    return str(configured).rstrip("/")


def get_oauth_redirect_uri(config=None):
    """Return the exact Spotify OAuth redirect URI used for login and token exchange."""
    config = config or load_config()
    public_base = get_oauth_public_base_url(config)
    configured = os.environ.get("SPOTIFY_REDIRECT_URI") or config.get("redirect_uri")
    redirect_uri = configured or f"{public_base}/callback"
    if _url_origin(redirect_uri) != _url_origin(public_base):
        raise OAuthOriginError("redirect_uri must share the public_base_url origin")
    parsed = urllib.parse.urlsplit(str(redirect_uri))
    if (
        parsed.path != "/callback"
        or "?" in str(redirect_uri)
        or "#" in str(redirect_uri)
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise OAuthOriginError("redirect_uri must be exactly public_base_url/callback")
    return str(redirect_uri)


def _origin_hostname_is_loopback(hostname):
    if not isinstance(hostname, str) or not hostname:
        return False
    folded = hostname.rstrip(".").casefold()
    if folded == "localhost" or folded.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(folded)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _phone_pairing_configuration(config=None):
    """Return bounded syntactic configuration state for phone OAuth pairing.

    This deliberately does not claim that DNS, TLS, proxy routing or Spotify's
    dashboard registration is reachable. Exact reasons are owner-only.
    """
    config = load_config() if config is None else config
    configured = os.environ.get("PUBLIC_BASE_URL") or config.get("public_base_url")
    if not configured:
        return {"configured": False, "reason": "public_origin_missing"}
    try:
        public_base = get_oauth_public_base_url(config)
        parsed = urllib.parse.urlsplit(public_base)
    except OAuthOriginError:
        return {"configured": False, "reason": "public_origin_invalid"}
    if _origin_hostname_is_loopback(parsed.hostname):
        return {"configured": False, "reason": "loopback_origin"}
    if parsed.scheme.lower() != "https":
        return {"configured": False, "reason": "https_required"}
    try:
        get_oauth_redirect_uri(config)
    except OAuthOriginError:
        return {"configured": False, "reason": "redirect_uri_invalid"}
    if not config.get("client_id") or not config.get("client_secret"):
        return {"configured": False, "reason": "client_credentials_missing"}
    return {"configured": True, "reason": None}


def _request_uses_public_origin(public_base):
    expected = _url_origin(public_base)
    if expected is None:
        return False
    if _url_origin(request.host_url) == expected:
        return True
    # Explicit PUBLIC_BASE_URL is the trust anchor for TLS-terminating reverse
    # proxies. Do not consume X-Forwarded-*; require their preserved Host to
    # match the configured public host/port instead.
    try:
        host = urllib.parse.urlsplit(f"//{request.host}")
        expected_host = urllib.parse.urlsplit(public_base)
        supplied_port = host.port
        expected_port = expected_host.port or (443 if expected_host.scheme == "https" else 80)
        default_port = 443 if expected_host.scheme == "https" else 80
        return (
            host.hostname
            and host.hostname.lower() == expected_host.hostname.lower()
            and (
                supplied_port == expected_port
                or (supplied_port is None and expected_port == default_port)
            )
        )
    except (TypeError, ValueError, AttributeError):
        return False


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


def play_uri_local(uri, skip_to_uri=None):
    """Start playback of a Spotify URI through go-librespot's local API.

    Pass skip_to_uri to begin a context (album/playlist) at a specific track,
    so the rest of the record keeps playing after it — go-librespot's
    /player/play accepts a `skip_to_uri` alongside the context `uri`.
    """
    if not uri or not uri.startswith("spotify:"):
        return False, "Invalid Spotify URI"

    payload = {"uri": uri}
    if skip_to_uri:
        payload["skip_to_uri"] = skip_to_uri

    try:
        resp = requests.post(
            f"{GO_LIBRESPOT_API_BASE}/player/play",
            json=payload,
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
    """Opt-in legacy fallback, strictly targeted to a configured Pi device."""
    token = get_user_token()
    if not token:
        return False, "No Spotify Web API token configured"

    headers = {"Authorization": f"Bearer {token}"}
    expected_device_id = (load_config().get("legacy_web_api_device_id") or "").strip()
    if not expected_device_id:
        return False, "Legacy fallback requires legacy_web_api_device_id"
    try:
        state_resp = requests.get(f"{SPOTIFY_API_BASE}/me/player", headers=headers, timeout=5)
        if state_resp.status_code != 200:
            return False, f"Could not verify active player: {state_resp.status_code}"
        state = state_resp.json()
        active_device_id = ((state.get("device") or {}).get("id") or "").strip()
        if not active_device_id or active_device_id != expected_device_id:
            return False, "Refusing to control a different Spotify device"
        params = {"device_id": expected_device_id}

        if action == "next":
            r = requests.post(f"{SPOTIFY_API_BASE}/me/player/next", headers=headers, params=params, timeout=5)
        elif action == "previous":
            r = requests.post(f"{SPOTIFY_API_BASE}/me/player/previous", headers=headers, params=params, timeout=5)
        elif action == "play-pause":
            if state.get("is_playing", False):
                r = requests.put(f"{SPOTIFY_API_BASE}/me/player/pause", headers=headers, params=params, timeout=5)
            else:
                r = requests.put(f"{SPOTIFY_API_BASE}/me/player/play", headers=headers, params=params, timeout=5)
        else:
            return False, "Unknown action"

        if r.status_code in (200, 202, 204):
            return True, "ok"
        return False, f"Spotify API error: {r.status_code}"
    except Exception as e:
        return False, str(e)


def control_playback(action):
    """Control playback locally; remote-device fallback is explicit opt-in."""
    ok, msg = control_playback_local(action)
    if ok:
        return True, msg

    if bool(load_config().get("allow_web_api_control_fallback", False)):
        return control_playback_web_api(action)

    return False, msg


# ── HTTP trust boundary ─────────────────────────────────────

_rate_lock = threading.Lock()
_rate_buckets = OrderedDict()


def _remote_is_loopback():
    try:
        address = ipaddress.ip_address(request.remote_addr or "")
        if address.is_loopback:
            return True
        mapped = getattr(address, "ipv4_mapped", None)
        return bool(mapped and mapped.is_loopback)
    except ValueError:
        return False


def _request_host_is_loopback():
    """Require a literal local Host; a DNS name can be rebound to loopback."""
    try:
        parsed = urllib.parse.urlsplit(f"//{request.host}")
        hostname = parsed.hostname or ""
        # Accessing port validates malformed/out-of-range values.
        parsed.port
    except (TypeError, ValueError):
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _backlight_request_is_trusted_local():
    # A reverse proxy normally connects from loopback and may rewrite Host to
    # its backend address. Forwarding markers therefore disqualify implicit
    # kiosk trust even when both socket/Host otherwise look local. A marker-free
    # proxy is indistinguishable at this layer and must preserve the public Host.
    forwarding_headers = (
        "Forwarded",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "Via",
    )
    return bool(
        _remote_is_loopback()
        and _request_host_is_loopback()
        and not any(request.headers.get(header) for header in forwarding_headers)
    )


def _owner_token():
    config = load_config()
    security = config.get("security") if isinstance(config.get("security"), dict) else {}
    return os.environ.get("OWNER_TOKEN") or security.get("owner_token") or config.get("owner_token") or ""


def _owner_session_binding(owner_token):
    secret = app.secret_key
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, bytes) or not isinstance(owner_token, str) or not owner_token:
        return ""
    return hmac.new(
        secret,
        b"spotify-display-owner-session\0" + owner_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _is_owner_request():
    if _backlight_request_is_trusted_local():
        return True
    expected = _owner_token()
    if not expected:
        return False
    session_binding = session.get("owner_token_binding")
    expected_binding = _owner_session_binding(expected)
    if (
        isinstance(session_binding, str)
        and session_binding
        and expected_binding
        and hmac.compare_digest(session_binding, expected_binding)
    ):
        return True
    supplied = request.headers.get("X-Owner-Token", "")
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    return bool(supplied) and hmac.compare_digest(str(supplied), str(expected))


def owner_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _is_owner_request():
            return jsonify({
                "error": "Owner authorization required",
                "hint": "Use the local kiosk or configure an OWNER_TOKEN",
            }), 401
        return fn(*args, **kwargs)
    return wrapped


def oauth_initiation_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        pairing = session.pop("oauth_pairing", None)
        if isinstance(pairing, dict):
            try:
                permitted_until = float(pairing.get("expires_at") or 0)
            except (TypeError, ValueError):
                permitted_until = 0
            epoch = pairing.get("profile_epoch")
            # Missing kind is a pre-v2 bounded pairing; never broaden it during
            # a rolling upgrade.
            profile_kind = pairing.get("profile_kind", "guest")
            current = _receiver_identity_snapshot()
            if (
                time.time() < permitted_until
                and epoch
                and profile_kind in ("household", "guest")
            ):
                if not (
                    current["active"]
                    and hmac.compare_digest(str(current["epoch"]), str(epoch))
                ):
                    raise ReceiverIdentityError(
                        "Spotify receiver changed before authorization started",
                        code="profile_changed",
                    )
                g.oauth_pairing_authorized = True
                g.oauth_profile_kind = profile_kind
                # Flask's signed cookie is readable by the browser. Keep raw
                # receiver/account identifiers server-side; only this opaque
                # random epoch crosses the cookie boundary.
                g.oauth_receiver_binding = {"profile_epoch": epoch}
                return fn(*args, **kwargs)
        if _is_owner_request():
            return fn(*args, **kwargs)
        return jsonify({"error": "An owner-approved pairing link is required"}), 401
    return wrapped


def _new_pairing_url(reuse=False, expected_epoch=None, profile_kind="household"):
    with _pairing_lock:
        public_base = get_oauth_public_base_url()
        if profile_kind not in ("household", "guest"):
            raise ValueError("profile_kind must be household or guest")
        pairing_config = _phone_pairing_configuration()
        if not pairing_config["configured"]:
            raise OAuthOriginError(
                f"Phone pairing unavailable ({pairing_config['reason']})"
            )
        context = _receiver_context()
        if context["profile_state"] == "no_receiver" or not context.get("receiver_alias"):
            raise ReceiverIdentityError(
                "Play on Pi Display before linking a Spotify library profile"
            )
        if expected_epoch is not None and not hmac.compare_digest(
            str(expected_epoch), str(context["profile_epoch"])
        ):
            raise ReceiverIdentityError(
                "Spotify receiver changed; refresh before creating a pairing link",
                code="profile_changed",
            )
        if (
            reuse
            and _kiosk_pairing.get("profile_epoch") == context["profile_epoch"]
            and _kiosk_pairing.get("profile_kind") in ("household", "guest")
        ):
            # Token consumption must not reset an owner-selected bounded guest
            # policy. Kiosk polling may rotate the one-use URL, but it retains
            # the selected policy for this receiver epoch.
            profile_kind = _kiosk_pairing["profile_kind"]
        if (
            reuse
            and _kiosk_pairing["url"]
            and _kiosk_pairing["expires_at"] > time.time() + 60
            and _kiosk_pairing.get("profile_epoch") == context["profile_epoch"]
            and _pairing_tokens.get(_kiosk_pairing["digest"]) is not None
        ):
            # An explicit owner-selected guest link remains authoritative when
            # the kiosk later asks to reuse its prompt.
            return _kiosk_pairing["url"]
        previous_digest = _kiosk_pairing.get("digest")
        if previous_digest:
            _pairing_tokens.pop(previous_digest, None)
        # Twelve Crockford/base32 characters carry 60 bits of entropy while
        # remaining practical to type from the physical display into a phone.
        alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        token = "".join(secrets.choice(alphabet) for _ in range(12))
        digest = hashlib.sha256(token.encode()).hexdigest()
        _pairing_tokens.set(digest, {
            "profile_epoch": context["profile_epoch"],
            "profile_kind": profile_kind,
        }, ttl=10 * 60)
        url = f"{public_base}/pair/{token}"
        _kiosk_pairing.update({
            "url": url,
            "digest": digest,
            "expires_at": time.time() + 10 * 60,
            "profile_epoch": context["profile_epoch"],
            "profile_kind": profile_kind,
        })
        return url


def _same_origin(url):
    supplied = _url_origin(url)
    if supplied is None:
        return False
    if supplied == _url_origin(request.host_url):
        return True
    # A configured public origin permits a TLS reverse proxy to forward to
    # loopback HTTP without trusting spoofable X-Forwarded-* headers.
    config = load_config()
    public_base = os.environ.get("PUBLIC_BASE_URL") or config.get("public_base_url")
    return bool(public_base) and supplied == _url_origin(public_base)


def _rate_limit(max_requests, window_seconds):
    now = time.monotonic()
    key = (request.remote_addr or "unknown", request.endpoint or request.path)
    with _rate_lock:
        bucket = _rate_buckets.setdefault(key, deque())
        while bucket and bucket[0] <= now - window_seconds:
            bucket.popleft()
        if len(bucket) >= max_requests:
            return False
        bucket.append(now)
        _rate_buckets.move_to_end(key)
        while len(_rate_buckets) > 1024:
            _rate_buckets.popitem(last=False)
    return True


def _mutation_rate_policy(path):
    """Return a bounded route-specific (requests, seconds) allowance."""
    if path in ("/api/control/volume", "/api/backlight"):
        # The kiosk coalesces these gestures at roughly 150ms. Leave margin
        # for retries/final-value delivery while retaining a finite ceiling.
        return 120, 10
    if path.startswith("/api/wled/") or path.startswith("/api/auth/"):
        return 10, 60
    if path.startswith("/api/control/"):
        return 40, 10
    return 40, 10


def _request_json_object():
    """Return a JSON object, never a truthy wrong-shaped JSON value."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


@app.before_request
def secure_request_boundary():
    _maybe_prune_expired_profiles()
    # Waitress imports the app rather than running __main__, so start daemon
    # workers lazily on the first real request. Tests explicitly skip this.
    if not app.testing and os.environ.get("SPOTIFY_DISPLAY_DISABLE_BACKGROUND") != "1":
        _start_background_services()

    if request.method == "GET" and request.path == "/api/lyrics":
        if not _rate_limit(10, 60):
            response = jsonify({"error": "Too many lyrics requests"})
            response.status_code = 429
            response.headers["Retry-After"] = "60"
            return response

    if request.method == "GET" and (
        request.path.startswith("/pair/")
        or (request.path == "/join" and request.args.get("pair"))
    ):
        if not _rate_limit(20, 60):
            response = jsonify({"error": "Too many pairing attempts"})
            response.status_code = 429
            response.headers["Retry-After"] = "60"
            return response

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # Browsers provide at least one of these signals. CLI/system clients
        # without browser headers remain backwards compatible on the LAN.
        if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return jsonify({"error": "Cross-site request rejected"}), 403
        origin = request.headers.get("Origin")
        referer = request.headers.get("Referer")
        if origin and not _same_origin(origin):
            return jsonify({"error": "Origin mismatch"}), 403
        if not origin and referer and not _same_origin(referer):
            return jsonify({"error": "Referer mismatch"}), 403

        limit, window = _mutation_rate_policy(request.path)
        if not _rate_limit(limit, window):
            return jsonify({"error": "Too many requests"}), 429


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; img-src 'self' https: data: blob:; connect-src 'self'",
    )
    if request.path.startswith((
        "/api/auth/",
        "/api/backlight",
        "/api/crate",
        "/api/idle/",
        "/api/wled",
        "/api/diagnostics",
        "/callback",
        "/login",
        "/join",
        "/pair/",
    )):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "Request body too large"}), 413


@app.errorhandler(ConfigWriteRefused)
def config_write_refused(error):
    return jsonify({
        "error": "Configuration repair required; write refused",
        "config": {"ok": False, "state": error.state, "writable": False},
    }), 503


@app.errorhandler(OAuthOriginError)
def oauth_origin_error(error):
    payload = {"error": str(error)}
    if error.expected_url:
        payload["expected_url"] = error.expected_url
    return jsonify(payload), error.status_code


@app.errorhandler(ReceiverIdentityError)
def receiver_identity_error(error):
    return jsonify({
        "error": str(error),
        "code": error.code,
        **_public_profile_context(),
    }), error.status_code


# ── UI routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/connect")
def connect():
    """Mobile-friendly page explaining how to connect to Pi Display."""
    return render_template("connect.html")


def _consume_pairing_token(token):
    if isinstance(token, str) and re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{12}", token.upper()):
        token = token.upper()
    digest = hashlib.sha256(str(token).encode()).hexdigest()
    binding = _pairing_tokens.pop(digest, None)
    if not isinstance(binding, dict):
        return jsonify({"error": "Pairing link is invalid, expired, or already used"}), 400
    with _pairing_lock:
        if _kiosk_pairing["digest"] == digest:
            # Retain profile_epoch/profile_kind so a normal kiosk poll cannot
            # replace an explicitly bounded guest link with a household link.
            _kiosk_pairing.update({
                "url": None,
                "digest": None,
                "expires_at": 0,
            })
    session["oauth_pairing"] = {
        **binding,
        "expires_at": time.time() + 5 * 60,
    }
    return redirect("/join")


@app.route("/pair/<token>")
def pair(token):
    """Consume a human-typeable one-use pairing token."""
    return _consume_pairing_token(token)


@app.route("/join")
def join():
    """Consume a legacy pairing query or show the household OAuth handoff."""
    token = request.args.get("pair", "")
    if token:
        return _consume_pairing_token(token)
    return render_template("join.html")


@app.route("/login")
@oauth_initiation_required
def login():
    """Start a state-bound OAuth flow using PKCE S256."""
    config = load_config()
    client_id = config.get("client_id", "")
    if not client_id:
        return "Spotify client_id is missing from config.json", 500
    paired = bool(getattr(g, "oauth_pairing_authorized", False))
    profile_kind = getattr(g, "oauth_profile_kind", "household")
    if profile_kind not in ("household", "guest"):
        profile_kind = "household"
    # Playback normally stays inside the loopback receiver; Web API playback
    # authority is requested only when its legacy fallback is explicitly on.
    requested_scopes = _oauth_scopes(config)
    scope = " ".join(requested_scopes)
    public_base = get_oauth_public_base_url(config)
    if not _request_uses_public_origin(public_base):
        raise OAuthOriginError(
            "Open OAuth from the configured public origin",
            expected_url=f"{public_base}/login",
            status_code=409,
        )
    redirect_uri = get_oauth_redirect_uri(config)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    session["spotify_oauth"] = {
        "state": state,
        "verifier": verifier,
        "created_at": time.time(),
        "paired": paired,
        "profile_kind": profile_kind,
        # Kept for safe deserialisation by an in-flight browser during a
        # rolling upgrade. New household pairings deliberately set False.
        "guest": profile_kind == "guest",
        "requested_scopes": requested_scopes,
        "receiver_binding": getattr(g, "oauth_receiver_binding", None),
    }
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return redirect(f"{SPOTIFY_AUTH_URL}?{params}")


@app.route("/callback")
def callback():
    """Complete a state-bound OAuth grant and replace account caches safely."""
    config = load_config()
    public_base = get_oauth_public_base_url(config)
    if not _request_uses_public_origin(public_base):
        raise OAuthOriginError(
            "OAuth callback arrived on a different origin",
            expected_url=get_oauth_redirect_uri(config),
            status_code=400,
        )
    code = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        reason = "access_denied" if error == "access_denied" else "provider_error" if error else "no_code"
        return jsonify({"error": "Authorization failed", "reason": reason}), 400

    flow = session.pop("spotify_oauth", None)
    supplied_state = request.args.get("state", "")
    if not isinstance(flow, dict) or not supplied_state:
        return jsonify({"error": "OAuth session is missing or expired"}), 400
    expected_state = str(flow.get("state") or "")
    try:
        flow_age = time.time() - float(flow.get("created_at") or 0)
    except (TypeError, ValueError):
        flow_age = float("inf")
    if flow_age < 0 or flow_age > 10 * 60 or not hmac.compare_digest(supplied_state, expected_state):
        return jsonify({"error": "Invalid or expired OAuth state"}), 400

    paired = bool(flow.get("paired", flow.get("guest")))
    profile_kind = flow.get("profile_kind")
    if profile_kind not in ("household", "guest"):
        profile_kind = "guest" if flow.get("guest") else "household"
    guest = profile_kind == "guest"
    binding = flow.get("receiver_binding")
    if paired and not isinstance(binding, dict):
        return jsonify({"error": "Paired OAuth is missing its receiver binding"}), 400
    if isinstance(binding, dict) and not _receiver_binding_matches(binding, refresh=True):
        return jsonify({
            "error": "Spotify receiver changed during authorization",
            "code": "profile_changed",
            **_public_profile_context(),
        }), 409

    redirect_uri = get_oauth_redirect_uri(config)

    try:
        resp = requests.post(SPOTIFY_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": flow.get("verifier", ""),
        }, auth=(config["client_id"], config["client_secret"]), timeout=10)

        if resp.status_code != 200:
            return jsonify({"error": "Token exchange failed", "status": resp.status_code}), 502

        data = resp.json()
        if not isinstance(data, dict):
            return jsonify({"error": "Spotify returned an invalid token response"}), 502
        refresh_token = data.get("refresh_token")
        access_token = data.get("access_token")
        if (
            not isinstance(refresh_token, str)
            or not refresh_token
            or not isinstance(access_token, str)
            or not access_token
        ):
            return jsonify({"error": "Spotify returned an incomplete token response"}), 502

        identity = _spotify_current_user(access_token)
        if not identity:
            return jsonify({"error": "Spotify account identity lookup failed"}), 502
        receiver_alias = identity["receiver_alias"]
        current = _receiver_identity_snapshot()
        if isinstance(binding, dict) and receiver_alias != current.get("alias"):
            return jsonify({
                "error": "The authorized Spotify account does not match the active receiver",
                "code": "receiver_account_mismatch",
            }), 409

        connected_at = time.time()
        authorized_at = connected_at
        reauthorize_at = reauthorization_deadline(authorized_at)
        if guest:
            try:
                hours = float(config.get("guest_session_hours", 12))
            except (TypeError, ValueError):
                hours = 12
            expires_at = connected_at + max(1, min(hours, 168)) * 60 * 60
        else:
            expires_at = None

        if isinstance(binding, dict) and not _receiver_binding_matches(binding, refresh=True):
            return jsonify({
                "error": "Spotify receiver changed before the profile could be linked",
                "code": "profile_changed",
                **_public_profile_context(),
            }), 409
        current = _receiver_identity_snapshot()
        if isinstance(binding, dict) and receiver_alias != current.get("alias"):
            return jsonify({
                "error": "The authorized Spotify account no longer matches the active receiver",
                "code": "receiver_account_mismatch",
            }), 409

        global _user_token, _user_token_expiry, _user_token_grant_id
        with _user_token_lock:
            profile = {
                "account_id": identity["account_id"],
                "display_name": identity["display_name"],
                "refresh_token": refresh_token,
                "kind": profile_kind,
                "connected_at": connected_at,
                "expires_at": expires_at,
                "authorized_at": authorized_at,
                "reauthorize_at": reauthorize_at,
                "scopes": _granted_scopes(
                    data.get("scope"), flow.get("requested_scopes") or _oauth_scopes(config)
                ),
                "receiver_aliases": [receiver_alias],
            }
            try:
                # A pre-profile grant has no trustworthy receiver binding. Keep
                # it quarantined until its own `/me.id` is verified by the
                # legacy migration path or an owner explicitly disconnects it.
                if isinstance(binding, dict):
                    committed_epoch = _persist_bound_profile_grant(
                        profile, receiver_alias, binding, clear_legacy=False
                    )
                    if not committed_epoch:
                        return jsonify({
                            "error": "Spotify receiver changed before profile publication",
                            "code": "profile_changed",
                            **_public_profile_context(),
                        }), 409
                else:
                    _persist_profile_grant(profile, receiver_alias, clear_legacy=False)
                    committed_epoch = None
            except (AliasCollisionError, ProfileLimitError) as error:
                code = "alias_collision" if isinstance(error, AliasCollisionError) else "profile_limit"
                return jsonify({"error": str(error), "code": code}), 409
            _clear_user_caches(identity["account_id"])
            expiry = time.time() + _token_lifetime(data.get("expires_in"))
            grant_id = hashlib.sha256(refresh_token.encode()).hexdigest()
            _user_tokens[identity["account_id"]] = {
                "access_token": access_token,
                "expires_at": expiry,
                "grant_id": grant_id,
            }
            _user_token = access_token
            _user_token_expiry = expiry
            _user_token_grant_id = grant_id
            current = _receiver_identity_snapshot()
            if committed_epoch is not None:
                if not (
                    current["active"]
                    and current["alias"] == receiver_alias
                    and hmac.compare_digest(str(current["epoch"]), committed_epoch)
                ):
                    return jsonify({
                        "error": "Spotify receiver changed after profile publication",
                        "code": "profile_changed",
                        **_public_profile_context(),
                    }), 409
            elif current["active"] and current["alias"] == receiver_alias:
                _bump_receiver_epoch(current["epoch"])

        return redirect("/connect?auth=ok")
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"OAuth callback failed: {e}")
        return jsonify({"error": "OAuth token exchange failed"}), 502


@app.route("/api/auth/owner", methods=["POST"])
def owner_login():
    """Exchange the configured owner token for a signed, HttpOnly session."""
    data = _request_json_object()
    if data is None:
        return jsonify({"error": "JSON object required"}), 400
    expected = _owner_token()
    supplied = data.get("token", "")
    if not expected:
        return jsonify({"error": "Remote owner access is not configured"}), 503
    if not supplied or not hmac.compare_digest(str(supplied), str(expected)):
        return jsonify({"error": "Invalid owner token"}), 401
    session.pop("owner", None)  # legacy boolean sessions are intentionally revoked
    session["owner_token_binding"] = _owner_session_binding(str(expected))
    return jsonify({"status": "ok"})


@app.route("/api/auth/pairing", methods=["POST"])
@owner_required
def create_pairing_link():
    """Create a one-use household (or explicit bounded guest) OAuth link."""
    data = _request_json_object()
    if request.data and data is None:
        return jsonify({"error": "JSON object required"}), 400
    initial = _receiver_context()
    expected_epoch = data.get("profile_epoch") if data else initial["profile_epoch"]
    profile_kind = data.get("profile_kind", "household") if data else "household"
    if profile_kind not in ("household", "guest"):
        return jsonify({"error": "profile_kind must be household or guest"}), 400
    join_url = _new_pairing_url(
        expected_epoch=expected_epoch, profile_kind=profile_kind
    )
    context = _receiver_context()
    if context["profile_epoch"] != initial["profile_epoch"]:
        with _pairing_lock:
            if _kiosk_pairing.get("profile_epoch") == initial["profile_epoch"]:
                _pairing_tokens.pop(_kiosk_pairing.get("digest"), None)
                _kiosk_pairing.update({
                    "url": None,
                    "digest": None,
                    "expires_at": 0,
                    "profile_epoch": None,
                    "profile_kind": None,
                })
        raise ReceiverIdentityError(
            "Spotify receiver changed while creating the pairing link",
            code="profile_changed",
        )
    return jsonify({
        "join_url": join_url,
        "expires_in": 10 * 60,
        "profile_epoch": context["profile_epoch"],
        "profile_kind": profile_kind,
    })


@app.route("/api/auth/status")
@owner_required
def auth_status():
    config = _prune_expired_profiles()
    store = _profile_store(config)
    context = _receiver_context(config)
    selected = _profile_by_id(
        context.get("account_id") or context.get("reauthorize_account_id"), config
    )
    profiles = []
    for account_id in sorted(store["profiles"]):
        stored = store["profiles"][account_id]
        profile = public_profile(stored)
        if profile:
            profile["reauth_required"] = _grant_reauthorization_due(stored)
            profiles.append(profile)
    return jsonify({
        "owner": True,
        "spotify_connected": bool(profiles or _legacy_grant(config)),
        "session_kind": selected.get("kind") if selected else None,
        "expires_at": selected.get("expires_at") if selected else None,
        "authorized_at": selected.get("authorized_at") if selected else None,
        "reauthorize_at": selected.get("reauthorize_at") if selected else None,
        "reauth_required": bool(context.get("reauth_required")),
        "legacy_grant_pending": bool(_legacy_grant(config)),
        "pairing": _phone_pairing_configuration(config),
        "profiles": profiles,
        **_public_profile_context(context, include_name=True),
    })


@app.route("/api/auth/disconnect", methods=["POST"])
@owner_required
def auth_disconnect():
    data = _request_json_object()
    if request.data and data is None:
        return jsonify({"error": "JSON object required"}), 400
    account_id = data.get("account_id") if data else None
    if account_id is not None and normalize_identifier(account_id) is None:
        return jsonify({"error": "Invalid account_id"}), 400
    _disconnect_user_account(account_id=account_id)
    session.pop("spotify_oauth", None)
    session.pop("oauth_pairing", None)
    return "", 204


# ── API routes ───────────────────────────────────────────────

@app.route("/api/now-playing")
def now_playing():
    """Return current playback state from the local Spotify Connect receiver."""
    state, available = read_playback_state_with_availability()
    context = _receiver_context()
    if state is None:
        if not available:
            response = jsonify({
                "error": "Playback receiver unavailable",
                **_public_profile_context(context),
            })
            response.status_code = 503
            response.headers["X-Spotify-Profile-State"] = context["profile_state"]
            response.headers["X-Spotify-Profile-Epoch"] = context["profile_epoch"] or ""
            return response
        response = Response(status=204)
        response.headers["X-Spotify-Profile-State"] = context["profile_state"]
        response.headers["X-Spotify-Profile-Epoch"] = context["profile_epoch"] or ""
        return response  # No content — nothing playing
    attach_album_extras(state)
    state.update(_public_profile_context(context))
    return jsonify(state)


@app.route("/api/health")
def health():
    """Return local receiver and fallback event health for troubleshooting."""
    go_available, go_state = read_go_librespot_state()
    state, state_path, state_error = _read_legacy_state_file()
    configuration = config_status()
    pairing_config = _phone_pairing_configuration()
    public_pairing = {"configured": pairing_config["configured"]}

    if state is None:
        healthy = go_available and configuration["ok"]
        payload = {
            "ok": healthy,
            "status": "config_error" if not configuration["ok"] else "healthy" if go_available else "unavailable",
            "config": configuration,
            "pairing": public_pairing,
            "go_librespot": {
                "available": go_available,
                "active": go_state is not None,
            },
            "raspotify_state": {
                "ok": False,
                "reason": state_error,
                "path": state_path,
            },
        }
        return jsonify(payload), 200 if healthy else 503

    timestamp = state.get("timestamp") or 0
    age = max(0, time.time() - timestamp) if timestamp else None
    event = state.get("event", "")
    is_playing = bool(state.get("is_playing", False))
    usable = bool(state.get("track_id")) and event not in STOPPED_IDLE_EVENTS
    stale_reason = None
    if age is None:
        usable = False
        stale_reason = "missing_timestamp"
    elif not is_playing and age > PAUSED_IDLE_AFTER_SECONDS:
        usable = False
        stale_reason = "paused_state_too_old"
    elif is_playing:
        try:
            duration = max(0, int(state.get("duration_ms") or 0))
            position = max(0, int(state.get("position_ms") or 0))
        except (TypeError, ValueError):
            duration = position = 0
        if duration and age * 1000 > max(0, duration - position) + END_OF_TRACK_GRACE_SECONDS * 1000:
            usable = False
            stale_reason = "past_expected_track_end"
        elif not duration and age > PLAYING_UNKNOWN_DURATION_STALE_SECONDS:
            usable = False
            stale_reason = "playing_state_too_old"

    playback_healthy = go_available or usable
    healthy = playback_healthy and configuration["ok"]
    payload = {
        "ok": healthy,
        "status": "config_error" if not configuration["ok"] else "healthy" if healthy else "stale",
        "config": configuration,
        "pairing": public_pairing,
        "go_librespot": {
            "available": go_available,
            "active": go_state is not None,
        },
        "raspotify_state": {
            "ok": usable,
            "present": True,
            "path": state_path,
            "event": event,
            "track_id": state.get("track_id"),
            "is_playing": is_playing,
            "position_ms": state.get("position_ms"),
            "duration_ms": state.get("duration_ms"),
            "volume_percent": state.get("volume_percent"),
            "age_seconds": None if age is None else round(age, 1),
            "stale_reason": stale_reason,
        },
    }
    return jsonify(payload), 200 if healthy else 503


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
    data = _request_json_object()
    if data is None:
        return jsonify({"error": "JSON object required"}), 400
    try:
        if isinstance(data.get("position_ms"), bool):
            raise ValueError
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
    data = _request_json_object()
    if data is None:
        return jsonify({"error": "JSON object required"}), 400
    try:
        if isinstance(data.get("percent"), bool):
            raise ValueError
        percent = max(0, min(100, int(data.get("percent"))))
    except (TypeError, ValueError):
        return jsonify({"error": "percent required"}), 400

    # Translate percent to go-librespot volume steps.
    steps_max = 100
    try:
        status = requests.get(f"{GO_LIBRESPOT_API_BASE}/status", timeout=1.5)
        if status.status_code == 200:
            status_payload = status.json()
            if isinstance(status_payload, dict):
                steps_max = max(1, min(65535, int(status_payload.get("volume_steps") or 100)))
    except (requests.RequestException, TypeError, ValueError, OverflowError):
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


@app.route("/api/backlight", methods=["GET", "POST"])
def backlight():
    """Read or request safe HID backlight state for the local kiosk.

    POST is deliberately loopback-only: unlike playback controls, changing the
    physical panel is not a LAN/API feature.  Accepted bodies are exactly one
    of ``{"percent": 0..100}`` or ``{"mode": "idle"|"active"}``; raw HID
    fields and device paths never cross this boundary.
    """
    if request.method == "GET":
        return jsonify(_get_backlight_controller().status(refresh=True))

    if not _backlight_request_is_trusted_local():
        return jsonify({"error": "Backlight control is only available to the local kiosk"}), 403

    data = _request_json_object()
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    allowed_keys = {"percent", "mode"}
    if set(data) - allowed_keys or ("percent" in data) == ("mode" in data):
        return jsonify({"error": "Provide exactly one of percent or mode"}), 400

    controller = _get_backlight_controller()
    try:
        if "percent" in data:
            state = controller.set_percent(data["percent"])
        elif data["mode"] == "idle":
            state = controller.set_idle()
        elif data["mode"] == "active":
            state = controller.set_active()
        else:
            return jsonify({"error": "mode must be idle or active"}), 400
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(state)


@app.route("/api/idle/playlists")
def idle_playlists():
    """Return house playlists for the idle launcher."""
    owner = _is_owner_request()
    context = _maybe_migrate_legacy_profile(_receiver_context())
    payload = idle_launcher_payload(
        include_private=owner and context["profile_state"] == "linked",
        account_id=context.get("account_id"),
        profile_epoch=context["profile_epoch"],
    )
    if not _crate_context_is_current(context):
        context = _receiver_context()
        payload = idle_launcher_payload(include_private=False)
    pairing_error = None
    try:
        if context["profile_state"] == "linked":
            # A linked kiosk has no pairing prompt to show. Avoid minting a
            # second authorization URL that could broaden an explicit guest.
            join_url = None
        else:
            pairing_config = _phone_pairing_configuration()
            if not pairing_config["configured"]:
                raise OAuthOriginError(
                    f"Phone pairing unavailable ({pairing_config['reason']})"
                )
            public_join = f"{get_oauth_public_base_url()}/join"
            join_url = (
                _new_pairing_url(reuse=True, expected_epoch=context["profile_epoch"])
                if _backlight_request_is_trusted_local() else public_join
            )
    except (OAuthOriginError, ReceiverIdentityError):
        join_url = None
        pairing_error = "Spotify phone pairing is temporarily unavailable"
    if not _crate_context_is_current(context):
        previous_epoch = context["profile_epoch"]
        context = _receiver_context()
        payload = idle_launcher_payload(include_private=False)
        if context["profile_epoch"] != previous_epoch:
            with _pairing_lock:
                if _kiosk_pairing.get("profile_epoch") == previous_epoch:
                    _pairing_tokens.pop(_kiosk_pairing.get("digest"), None)
                    _kiosk_pairing.update({
                        "url": None,
                        "digest": None,
                        "expires_at": 0,
                        "profile_epoch": None,
                        "profile_kind": None,
                    })
            join_url = None
            pairing_error = "Spotify receiver changed; refresh to create a new pairing link"
    response = {
        "playlists": payload["playlists"],
        "title": payload["title"],
        # Only the kiosk itself can mint the one-use household authorization URL.
        # A remote unauthenticated caller sees the informational join page.
        "join_url": join_url,
        "pairing_error": pairing_error,
        **_public_profile_context(context, include_name=owner),
    }
    return jsonify(response)


@app.route("/api/crate")
@owner_required
def crate():
    """Sections of browsable music for the kiosk crate UI."""
    return jsonify(crate_payload())


@app.route("/api/idle/play", methods=["POST"])
def idle_play():
    """Start playback from a crate / idle launcher card."""
    data = _request_json_object()
    if data is None:
        return jsonify({"error": "JSON object required"}), 400
    uri = data.get("uri", "")
    if not isinstance(uri, str):
        return jsonify({"error": "uri must be a string"}), 400
    supplied_epoch = data.get("profile_epoch")
    context = _maybe_migrate_legacy_profile(_receiver_context())
    if not isinstance(supplied_epoch, str) or not hmac.compare_digest(
        supplied_epoch, str(context["profile_epoch"])
    ):
        return _profile_changed_response(context)
    owner = _is_owner_request()
    allowed = {
        item["uri"] for item in idle_launcher_payload(
            include_private=owner and context["profile_state"] == "linked",
            account_id=context.get("account_id"),
            profile_epoch=context["profile_epoch"],
        )["playlists"]
    }
    if owner:
        crate = crate_payload(context)
        if crate.get("profile_epoch") != supplied_epoch:
            return _profile_changed_response()
        for section in crate["sections"]:
            allowed.update(item["uri"] for item in section["items"])
    if uri not in allowed:
        return jsonify({"error": "Playlist is not configured for this display"}), 400

    if not _crate_context_is_current(context):
        return _profile_changed_response()

    ok, msg = play_uri_local(uri)
    if ok:
        return jsonify({"status": "ok"})
    status = 503 if "unavailable" in msg.lower() or "not ready" in msg.lower() else 502
    return jsonify({"error": msg}), status


@app.route("/api/album/tracks")
def album_tracks():
    """Tracklist for an album — defaults to whatever is playing now.

    Scoped to the current record: an explicit ?album_id must match what's on
    the platter, so this can't be used as an open Spotify metadata proxy.
    """
    playing = current_album_id()
    requested = request.args.get("album_id", "").strip()
    album_id = requested or playing
    if not album_id:
        return jsonify({"album_id": None, "tracks": []})  # not enriched yet
    if requested and requested != playing:
        return jsonify({"error": "Album is not currently playing"}), 409

    tracks = lookup_album_tracks(album_id)
    return jsonify({"album_id": album_id, "tracks": tracks})


@app.route("/api/album/play-track", methods=["POST"])
def album_play_track():
    """Play a track from the currently playing album, keeping album context.

    Only tracks that belong to the record on the platter are accepted, so the
    endpoint can't start arbitrary playback.
    """
    data = _request_json_object()
    if data is None:
        return jsonify({"error": "JSON object required"}), 400
    track_uri = data.get("uri", "")
    if not isinstance(track_uri, str) or not track_uri.startswith("spotify:track:"):
        return jsonify({"error": "Invalid track URI"}), 400

    album_id = current_album_id()
    if not album_id:
        return jsonify({"error": "Nothing is playing"}), 409

    tracks = lookup_album_tracks(album_id)
    if track_uri not in {t["uri"] for t in tracks}:
        return jsonify({"error": "Track is not on the current album"}), 400

    ok, msg = play_uri_local(f"spotify:album:{album_id}", skip_to_uri=track_uri)
    if ok:
        return jsonify({"status": "ok"})
    status = 503 if "unavailable" in msg.lower() or "not ready" in msg.lower() else 502
    return jsonify({"error": msg}), status


@app.route("/api/info")
def info():
    """Return server info including the LAN URL."""
    ip = get_local_ip()
    return jsonify({"ip": ip, "port": SERVER_PORT, "url": f"http://{ip}:{SERVER_PORT}"})


def _playback_event_signal(state, receiver_available=True):
    item = (state or {}).get("item") or {}
    progress = int((state or {}).get("progress_ms") or 0)
    identity = item.get("id") or item.get("uri")
    if not identity and item.get("name"):
        artists = ",".join(str(a.get("name") or "") for a in (item.get("artists") or []))
        identity = f"local:{item.get('name')}:{artists}:{item.get('duration_ms') or 0}"
    return {
        "active": bool(identity),
        "track_id": identity,
        "is_playing": bool((state or {}).get("is_playing", False)),
        # The UI interpolates progress locally; a 10s bucket detects lost
        # handoffs/seeks without recreating the old two-second poll load.
        "progress_bucket": progress // 10000,
        "receiver_available": bool(receiver_available),
        **_public_profile_context(),
    }


def _ensure_event_monitor():
    global _event_monitor_started
    with _event_condition:
        if _event_monitor_started:
            return
        _event_monitor_started = True

    def monitor():
        global _event_version, _event_signal
        previous = object()
        while True:
            try:
                state, available = read_playback_state_with_availability()
                signal = _playback_event_signal(state, receiver_available=available)
            except Exception as e:
                print(f"Playback event monitor error: {e}")
                time.sleep(1)
                continue
            signature = tuple(sorted(signal.items()))
            if signature != previous:
                previous = signature
                with _event_condition:
                    _event_version += 1
                    _event_signal = {"version": _event_version, **signal}
                    _event_condition.notify_all()
            time.sleep(1)

    threading.Thread(target=monitor, name="playback-events", daemon=True).start()


@app.route("/api/events")
def events():
    """SSE playback-change signals; clients refetch /api/now-playing."""
    global _event_clients
    with _event_condition:
        if _event_clients >= MAX_SSE_CLIENTS:
            response = jsonify({"error": "Too many event-stream clients"})
            response.status_code = 503
            response.headers["Retry-After"] = "10"
            return response
        _event_clients += 1

    released = False

    def release_slot():
        nonlocal released
        global _event_clients
        with _event_condition:
            if released:
                return
            released = True
            _event_clients = max(0, _event_clients - 1)

    try:
        _ensure_event_monitor()
    except Exception:
        release_slot()
        raise

    @stream_with_context
    def generate():
        seen = -1
        try:
            while True:
                with _event_condition:
                    if seen == _event_version:
                        _event_condition.wait(timeout=15)
                    signal = dict(_event_signal)
                    version = _event_version
                if version != seen:
                    seen = version
                    yield f"event: playback\ndata: {json.dumps(signal, separators=(',', ':'))}\n\n"
                else:
                    yield ": keepalive\n\n"
        finally:
            release_slot()

    response = Response(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.call_on_close(release_slot)
    return response


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
WLED_SCAN_DEMAND_SECONDS = 10
WLED_PROBE_TIMEOUT = 0.4
WLED_PROBE_CONCURRENCY = 32
WLED_SCAN_BATCH_SIZE = 512


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


def _probe_wled(host):
    try:
        resp = requests.get(f"http://{host}/json/info", timeout=WLED_PROBE_TIMEOUT)
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
    try:
        pixel_count = int(leds.get("count") or 0) or None
    except (TypeError, ValueError, OverflowError):
        pixel_count = None
    if pixel_count is not None:
        pixel_count = max(1, min(pixel_count, MAX_WLED_PIXELS))
    try:
        parsed = urllib.parse.urlsplit(f"//{host}")
        port = parsed.port or 80
    except ValueError:
        port = 80
    return (info.get("name") or host, host, port, pixel_count)


def _local_scan_network():
    override = os.environ.get("WLED_SCAN_CIDR")
    if override:
        try:
            network = ipaddress.ip_network(override, strict=False)
            return network if isinstance(network, ipaddress.IPv4Network) else None
        except ValueError:
            return None

    local = get_local_ip()
    prefix = 24
    try:
        # Linux exposes the active route's real netmask here. This correctly
        # discovers /22 and similar LANs without adding a platform dependency.
        with open("/proc/net/route", "r") as routes:
            for line in routes.read().splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 8 and fields[1] == "00000000":
                    mask = socket.inet_ntoa(bytes.fromhex(fields[7])[::-1])
                    prefix = ipaddress.ip_network(f"0.0.0.0/{mask}").prefixlen
                    break
    except (OSError, ValueError):
        pass
    # Avoid accidentally probing an enterprise /16; /22 still covers the
    # deployed LAN while bounding work to 1,022 hosts.
    prefix = max(prefix, 22)
    try:
        return ipaddress.ip_network(f"{local}/{prefix}", strict=False)
    except ValueError:
        return None


def _request_wled_scan(now=None):
    """Wake the scanner for a short, coalesced discovery-demand window.

    The kiosk calls the discovery endpoint only while its setup UI is useful
    (idle or explicitly open). Keeping demand shorter than the frontend's
    refresh interval means one abandoned request cannot leave a permanent LAN
    sweep behind. Repeated requests extend the window but the scan interval
    and single-flight state still bound work.
    """
    now = time.monotonic() if now is None else now
    with _wled_scan_condition:
        _wled_scan_state["demand_until"] = max(
            _wled_scan_state["demand_until"],
            now + WLED_SCAN_DEMAND_SECONDS,
        )
        _wled_scan_condition.notify_all()


def _claim_wled_scan(now=None):
    """Atomically claim a due scan; exposed separately for deterministic tests."""
    now = time.monotonic() if now is None else now
    with _wled_scan_condition:
        if _wled_scan_state["running"]:
            return False
        if now >= _wled_scan_state["demand_until"]:
            return False
        if now < _wled_scan_state["last_started"] + WLED_SCAN_INTERVAL_SECONDS:
            return False
        _wled_scan_state["running"] = True
        _wled_scan_state["last_started"] = now
        return True


def _finish_wled_scan():
    with _wled_scan_condition:
        _wled_scan_state["running"] = False
        _wled_scan_condition.notify_all()


def _wled_scan_wait_timeout(now):
    """Return None while dormant, otherwise seconds until due/expiry."""
    with _wled_scan_condition:
        demand_remaining = _wled_scan_state["demand_until"] - now
        if demand_remaining <= 0:
            return None
        interval_remaining = (
            _wled_scan_state["last_started"]
            + WLED_SCAN_INTERVAL_SECONDS
            - now
        )
        return max(0.0, min(demand_remaining, interval_remaining))


def _scan_wled_lan_batch(previous_network, scan_cursor):
    """Probe one bounded subnet batch and return updated network/cursor state."""
    local = get_local_ip()
    network = _local_scan_network()
    if network is None:
        return previous_network, scan_cursor
    if network != previous_network:
        print(f"WLED scan: demand-driven discovery on {network}")
        previous_network = network
        scan_cursor = 0

    # Reserve part of the fixed request budget for configured mDNS names so a
    # large subnet never turns the "bounded batch" into 512 plus N requests.
    configured = _wled_config_devices(load_config().get("wled") or {})
    configured_hosts = list(dict.fromkeys(device["host"] for device in configured))[
        :MAX_WLED_DEVICES
    ]
    subnet_batch_size = max(0, WLED_SCAN_BATCH_SIZE - len(configured_hosts))

    all_hosts = [str(h) for h in network.hosts() if str(h) != local]
    if subnet_batch_size == 0:
        targets = []
    elif len(all_hosts) > subnet_batch_size:
        targets = all_hosts[scan_cursor:scan_cursor + subnet_batch_size]
        if len(targets) < subnet_batch_size:
            targets += all_hosts[:subnet_batch_size - len(targets)]
        scan_cursor = (scan_cursor + subnet_batch_size) % len(all_hosts)
    else:
        targets = all_hosts

    # Probe configured mDNS names as well as port 80 across the subnet. Put
    # those first so known strips are refreshed promptly even on a /22 batch.
    targets = list(dict.fromkeys(configured_hosts + targets))
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=WLED_PROBE_CONCURRENCY
    ) as executor:
        for result in executor.map(_probe_wled, targets):
            if result:
                name, ip, port, pixel_count = result
                _wled_record_device(name, ip, port, pixel_count)
    return previous_network, scan_cursor


def _start_wled_lan_scanner():
    """Start a dormant worker that scans only after recent discovery demand.

    Replaces an earlier mDNS-based browser that bound to UDP 5353 and
    conflicted with avahi-daemon. go-librespot uses avahi for Spotify
    Connect advertisement; the dual-bind triggered a spam of
    "failed handling zeroconf add user request" and intermittent
    receiver dropouts. An HTTP scan touches no shared sockets and
    catches every WLED that's reachable on the LAN.
    """

    with _wled_scan_condition:
        if _wled_scan_state["worker_started"]:
            return False
        _wled_scan_state["worker_started"] = True

    def _run():
        previous_network = None
        scan_cursor = 0
        while True:
            # Keep the scheduling decision and condition wait under the same
            # lock so a request cannot be lost between checking and sleeping.
            with _wled_scan_condition:
                now = time.monotonic()
                if not _claim_wled_scan(now):
                    timeout = _wled_scan_wait_timeout(now)
                    _wled_scan_condition.wait(timeout=timeout)
                    continue
            try:
                previous_network, scan_cursor = _scan_wled_lan_batch(
                    previous_network,
                    scan_cursor,
                )
            except Exception as e:
                print(f"WLED scan error: {e}")
            finally:
                _finish_wled_scan()

    try:
        t = threading.Thread(target=_run, name="wled-lan-scan", daemon=True)
        t.start()
    except Exception:
        with _wled_scan_condition:
            _wled_scan_state["worker_started"] = False
        raise
    return True


@app.route("/api/wled/discovered")
@owner_required
def wled_discovered():
    """Return WLED devices currently visible on the LAN."""
    _request_wled_scan()
    return jsonify({"devices": _wled_active_devices()})


def _wled_config_devices(wled):
    """Same shape as wled_sync._normalize_devices — kept here to avoid a
    cross-module import. Returns a list of {host, name, pixel_count}."""
    raw = wled.get("devices")
    out = []
    if isinstance(raw, list) and raw:
        for entry in raw[:MAX_WLED_DEVICES]:
            if not isinstance(entry, dict):
                continue
            try:
                out.append(_validate_wled_device(entry))
            except ValueError:
                continue
        return out
    legacy_host = (wled.get("host") or "").strip()
    if legacy_host:
        try:
            out.append(_validate_wled_device({
                "host": legacy_host,
                "name": wled.get("name") or legacy_host,
                "pixel_count": wled.get("pixel_count") or 46,
            }))
        except ValueError:
            pass
    return out


MAX_WLED_DEVICES = 16
MAX_WLED_PIXELS = 2048
MAX_WLED_NAME_LENGTH = 64
MAX_WLED_HOST_LENGTH = 253


def _bounded_float(value, default, minimum, maximum, field):
    try:
        result = float(default if value is None else value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be numeric")
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return result


def _validate_wled_host(value):
    if not isinstance(value, str):
        raise ValueError("host must be a string")
    host = value.strip()
    if not host or len(host) > MAX_WLED_HOST_LENGTH or any(c.isspace() for c in host):
        raise ValueError("host is missing or too long")
    # wled_sync sends UDP to this value, so an HTTP URL or :port suffix is not
    # meaningful here. Discovery keeps its HTTP port as separate metadata.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        raise ValueError("host must be a bare LAN IPv4 address or DNS/mDNS name")
    hostname = host
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if hostname.startswith((".", "-")) or hostname.endswith((".", "-")) or ".." in hostname:
            raise ValueError("host is not a valid hostname")
    else:
        if not (address.is_private or address.is_link_local or address.is_loopback):
            raise ValueError("WLED IP must be on the local network")
    return host


def _validate_wled_device(entry):
    if not isinstance(entry, dict):
        raise ValueError("each device must be an object")
    host = _validate_wled_host(entry.get("host"))
    name_value = entry.get("name") or host
    if not isinstance(name_value, str):
        raise ValueError("name must be a string")
    name = name_value.strip()
    if not name or len(name) > MAX_WLED_NAME_LENGTH:
        raise ValueError(f"name must be 1-{MAX_WLED_NAME_LENGTH} characters")
    pixels = entry.get("pixel_count", 46)
    if isinstance(pixels, bool):
        raise ValueError("pixel_count must be an integer")
    try:
        pixels = int(pixels)
    except (TypeError, ValueError):
        raise ValueError("pixel_count must be an integer")
    if not 1 <= pixels <= MAX_WLED_PIXELS:
        raise ValueError(f"pixel_count must be between 1 and {MAX_WLED_PIXELS}")
    reverse = entry.get("reverse", False)
    if not isinstance(reverse, bool):
        raise ValueError("reverse must be true or false")
    return {
        "host": host,
        "name": name,
        "pixel_count": pixels,
        "reverse": reverse,
        "phase_offset": _bounded_float(entry.get("phase_offset"), 0, -1, 1, "phase_offset"),
        "brightness": _bounded_float(entry.get("brightness"), 1, 0.05, 1, "brightness"),
        "gamma": _bounded_float(entry.get("gamma"), 1, 0.5, 3, "gamma"),
    }


@app.route("/api/wled/status")
@owner_required
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
@owner_required
def wled_devices_update():
    """Atomically replace the configured WLED device list.

    Body: {"devices": [{"host": "...", "name": "...", "pixel_count": 46}], "enabled": bool}
    Sending {"devices": []} clears the list (and effectively releases WLED on
    the next wled_sync tick); `enabled` is optional and defaults to True when
    devices is non-empty.
    """
    data = _request_json_object()
    if data is None:
        return jsonify({"error": "JSON object required"}), 400
    incoming = data.get("devices")
    if not isinstance(incoming, list):
        return jsonify({"error": "devices must be a list"}), 400

    if len(incoming) > MAX_WLED_DEVICES:
        return jsonify({"error": f"at most {MAX_WLED_DEVICES} devices are supported"}), 400
    try:
        devices = [_validate_wled_device(entry) for entry in incoming]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if "enabled" in data and not isinstance(data["enabled"], bool):
        return jsonify({"error": "enabled must be true or false"}), 400

    def store(latest):
        wled = latest.get("wled") if isinstance(latest.get("wled"), dict) else {}
        wled["devices"] = devices
        for legacy_key in ("host", "name", "pixel_count"):
            wled.pop(legacy_key, None)
        if "enabled" in data:
            wled["enabled"] = data["enabled"]
        elif devices and not wled.get("enabled"):
            wled["enabled"] = True
        latest["wled"] = wled

    update_config(store)
    return "", 204


def _cpu_temperature_c():
    for path in ("/sys/class/thermal/thermal_zone0/temp", "/sys/class/hwmon/hwmon0/temp1_input"):
        try:
            with open(path, "r") as f:
                return round(float(f.read().strip()) / 1000, 1)
        except (OSError, ValueError):
            continue
    return None


def _read_wled_runtime_status():
    """Read the service-owned status file with strict size/schema/type bounds."""
    def safe_int(value, maximum=1_000_000_000):
        try:
            return max(0, min(int(value or 0), maximum))
        except (TypeError, ValueError, OverflowError):
            return 0

    def safe_number(value):
        try:
            value = float(value)
            return value if math.isfinite(value) and 0 <= value <= 86400 else None
        except (TypeError, ValueError, OverflowError):
            return None

    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(WLED_STATUS_FILE, flags)
        try:
            file_stat = os.fstat(fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_size <= 0
                or file_stat.st_size > MAX_RUNTIME_STATE_BYTES
            ):
                return {"ok": False, "reason": "invalid_size"}
            with os.fdopen(fd, "rb") as status_file:
                fd = None
                raw = status_file.read(MAX_RUNTIME_STATE_BYTES + 1)
        finally:
            if fd is not None:
                os.close(fd)
        if len(raw) > MAX_RUNTIME_STATE_BYTES:
            return {"ok": False, "reason": "invalid_size"}
        data = json.loads(raw)
    except FileNotFoundError:
        return {"ok": False, "reason": "status_file_missing"}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "reason": "status_file_invalid"}
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return {"ok": False, "reason": "unsupported_schema"}
    try:
        updated = float(data.get("updated_unix"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "missing_timestamp"}
    age = time.time() - updated
    if age < -60:
        return {"ok": False, "reason": "future_timestamp"}

    playback = data.get("playback") if isinstance(data.get("playback"), dict) else {}
    safe_playback = {
        "state": str(playback.get("state") or "unknown")[:32],
        "thread_alive": bool(playback.get("thread_alive", False)),
        "consecutive_failures": safe_int(playback.get("consecutive_failures"), 1_000_000),
        "last_error": str(playback.get("last_error") or "")[:256] or None,
        "last_poll_age_seconds": safe_number(playback.get("last_poll_age_seconds")),
        "last_success_age_seconds": safe_number(playback.get("last_success_age_seconds")),
        "failure_grace_seconds": safe_number(playback.get("failure_grace_seconds")),
    }
    hosts = data.get("hosts_seen") if isinstance(data.get("hosts_seen"), list) else []
    stale = age > 30
    return {
        "ok": not stale,
        "stale": stale,
        "reason": "stale" if stale else None,
        "age_seconds": round(max(0, age), 1),
        "enabled": bool(data.get("enabled", False)),
        "configured_devices": safe_int(data.get("configured_devices"), MAX_WLED_DEVICES),
        "rendering": bool(data.get("rendering", False)),
        "spin_speed": safe_number(data.get("spin_speed")),
        "udp_datagrams_queued": safe_int(data.get("udp_datagrams_queued")),
        "udp_local_send_errors": safe_int(data.get("udp_local_send_errors")),
        "hosts_seen": [str(host)[:MAX_WLED_HOST_LENGTH] for host in hosts[:MAX_WLED_DEVICES]],
        "playback": safe_playback,
    }


@app.route("/api/diagnostics")
@owner_required
def diagnostics():
    """Bounded component health for the hidden owner diagnostics panel."""
    go_available, go_state = read_go_librespot_state()
    disk = shutil.disk_usage(BASE_DIR)
    try:
        load_average = [round(value, 2) for value in os.getloadavg()]
    except (AttributeError, OSError):
        load_average = None
    config = _prune_expired_profiles()
    wled = config.get("wled") or {}
    profile_context = _receiver_context(config)
    active_crate_cache = _crate_cache_for(profile_context["cache_key"])
    caches = {
        "tracks": len(_track_cache),
        "albums": len(_album_cache),
        "album_tracks": len(_album_tracks_cache),
        "artists": len(_artist_albums_cache),
        "artwork": len(_uri_image_cache),
        "lyrics": len(_lyrics_cache),
    }
    return jsonify({
        "uptime_seconds": round(time.time() - _started_at, 1),
        "server": {"port": SERVER_PORT, "pid": os.getpid()},
        "config": config_status(),
        "receiver": {"available": go_available, "active": go_state is not None},
        "events": {"version": _event_version, "clients": _event_clients, "max_clients": MAX_SSE_CLIENTS},
        "crate": {
            "building": _crate_building,
            "account_generation": _account_generation,
            "profile_state": profile_context["profile_state"],
            "profile_epoch": profile_context["profile_epoch"],
            "cache_count": len(_crate_caches),
            "age_seconds": round(max(0, time.time() - active_crate_cache["built_at"]), 1)
            if active_crate_cache["built_at"] else None,
        },
        "wled": {
            "enabled": bool(wled.get("enabled", False)),
            "configured_devices": len(_wled_config_devices(wled)),
            "discovered_devices": len(_wled_active_devices()),
            "runtime": _read_wled_runtime_status(),
        },
        "lyrics": {
            "circuit_open": time.time() < _lyrics_breaker_until,
            "retry_after": max(0, round(_lyrics_breaker_until - time.time(), 1)),
        },
        "system": {
            "cpu_temperature_c": _cpu_temperature_c(),
            "load_average": load_average,
            "disk_free_bytes": disk.free,
            "disk_total_bytes": disk.total,
        },
        "caches": caches,
    })


@app.route("/api/lyrics")
def lyrics():
    """Fetch/cache LRCLIB lyrics, scoped to the record currently displayed."""
    track_name = request.args.get("track", "").strip()
    artist_name = request.args.get("artist", "").strip()
    album_name = request.args.get("album", "").strip()
    duration_raw = request.args.get("duration", "0")
    if not track_name or not artist_name:
        return jsonify({"error": "Missing track/artist"}), 400
    if any(len(value) > 200 for value in (track_name, artist_name, album_name)):
        return jsonify({"error": "Lyrics query is too long"}), 400
    try:
        duration = max(0, min(24 * 60 * 60, int(float(duration_raw or 0))))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid duration"}), 400

    state = read_playback_state()
    item = (state or {}).get("item") or {}
    current_track = str(item.get("name") or "").strip()
    current_artists = [str(a.get("name") or "").strip() for a in (item.get("artists") or [])]
    current_album = str((item.get("album") or {}).get("name") or "").strip()
    normalized_artists = {name.casefold() for name in current_artists if name}
    if current_artists:
        normalized_artists.add(", ".join(current_artists).casefold())
    if (
        not (item.get("id") or item.get("uri"))
        or track_name.casefold() != current_track.casefold()
        or artist_name.casefold() not in normalized_artists
        or (album_name and current_album and album_name.casefold() != current_album.casefold())
    ):
        return jsonify({"error": "Lyrics are only available for the current track"}), 409

    cache_key = str(item.get("id") or item.get("uri"))
    cached = _lyrics_cache.get(cache_key)
    if cached is not None:
        return jsonify({**cached, "cached": True})

    global _lyrics_breaker_until
    with _lyrics_lock:
        if time.time() < _lyrics_breaker_until:
            return jsonify({
                "syncedLyrics": "",
                "plainLyrics": "",
                "status": "temporarily_unavailable",
                "retry_after": max(1, int(_lyrics_breaker_until - time.time())),
            })
        if cache_key in _lyrics_inflight:
            response = jsonify({
                "syncedLyrics": "",
                "plainLyrics": "",
                "status": "pending",
            })
            response.status_code = 202
            response.headers["Retry-After"] = "1"
            return response
        _lyrics_inflight.add(cache_key)
    try:
        try:
            resp = requests.get("https://lrclib.net/api/get", params={
                "track_name": track_name,
                "artist_name": artist_name,
                "album_name": album_name,
                "duration": duration,
            }, headers={"User-Agent": "SpotifyPiDisplay/2.0"}, timeout=3.5)
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError("LRCLIB returned invalid JSON")
                payload = {
                    "syncedLyrics": data.get("syncedLyrics") or "",
                    "plainLyrics": data.get("plainLyrics") or "",
                    "status": "ok",
                }
                _lyrics_cache.set(cache_key, payload, ttl=24 * 60 * 60)
                return jsonify({**payload, "cached": False})
            if resp.status_code in (404, 400):
                payload = {"syncedLyrics": "", "plainLyrics": "", "status": "not_found"}
                _lyrics_cache.set(cache_key, payload, ttl=10 * 60)
                return jsonify({**payload, "cached": False})
            raise requests.RequestException(f"LRCLIB returned {resp.status_code}")
        except (requests.RequestException, ValueError) as e:
            print(f"Lyrics lookup failed: {e}")
            now = time.time()
            with _lyrics_lock:
                while _lyrics_failure_times and _lyrics_failure_times[0] < now - 60:
                    _lyrics_failure_times.popleft()
                _lyrics_failure_times.append(now)
                if len(_lyrics_failure_times) >= 3:
                    _lyrics_breaker_until = now + 60
            payload = {
                "syncedLyrics": "",
                "plainLyrics": "",
                "status": "upstream_error",
            }
            return jsonify(payload)
    finally:
        with _lyrics_lock:
            _lyrics_inflight.discard(cache_key)


def _start_background_services():
    global _background_started
    with _background_lock:
        if _background_started:
            return
        components = (
            ("backlight", lambda: _get_backlight_controller().start()),
            ("wled_scanner", _start_wled_lan_scanner),
            ("crate", _rebuild_crate_async),
            ("events", _ensure_event_monitor),
        )
        for name, starter in components:
            if name in _background_components_started:
                continue
            try:
                starter()
            except Exception as error:
                # Keep unrelated components alive and retry only the failed
                # starter on the next request.
                print(f"Background component {name} failed to start: {error}")
                continue
            _background_components_started.add(name)
        _background_started = len(_background_components_started) == len(components)


if __name__ == "__main__":
    _start_background_services()
    # Production listens on the LAN through the hardened waitress service.
    # The development server is loopback-only unless explicitly overridden.
    app.run(host=os.environ.get("BIND_HOST", "127.0.0.1"), port=SERVER_PORT, debug=False)
