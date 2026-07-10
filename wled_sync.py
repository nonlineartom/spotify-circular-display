#!/usr/bin/env python3
"""WLED ambient sync — mirror album-art colors and track progress on a WLED strip.

While Spotify is playing, this service streams a full per-pixel buffer to WLED
over UDP DRGB at a few Hz. The buffer is a gradient interpolated across the
extracted album-art palette, with a small "dim band" whose position along the
strip reflects how far through the track we are.

When playback is paused, the gradient decelerates smoothly and then holds while
the progress band freezes. When truly idle (no track, or Spotify disconnected),
this service stops sending packets entirely — WLED's realtime mode times out
after `realtime_timeout_seconds` and reverts to whatever the user configured
on the device itself.

Configuration is re-read from `config.json` every two seconds, so changes made
via the kiosk's WLED setup UI take effect without a service restart.
"""

from __future__ import annotations

import colorsys
import io
import ipaddress
import json
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import requests

try:
    from PIL import Image
except ImportError:
    print("wled_sync: Pillow is required (pip install Pillow)", file=sys.stderr)
    raise


HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
NOW_PLAYING_URL = os.environ.get(
    "WLED_NOW_PLAYING_URL", "http://127.0.0.1:5000/api/now-playing"
)
WLED_UDP_PORT = 21324
DRGB_PROTOCOL_ID = 2

NOW_PLAYING_POLL_SECONDS = 2.0
CONFIG_RELOAD_SECONDS = 2.0
CROSSFADE_SECONDS = 1.0
# Matches SPIN_RAMP in templates/index.html — the vinyl on the display
# eases its rotation speed up/down over this many seconds. The WLED
# gradient mirrors that ramp so the lights spin in lockstep with the
# record (1:1 spin-up on play, spin-down on pause).
SPIN_RAMP_SECONDS = 4.0
PLAYBACK_FAILURE_GRACE_SECONDS = float(
    os.environ.get("WLED_PLAYBACK_FAILURE_GRACE_SECONDS", "8")
)
STATUS_FILE = os.environ.get(
    "WLED_STATUS_FILE", "/run/spotify-display/wled-status.json"
)

MAX_DEVICES = 16
MAX_PIXELS_PER_DEVICE = 2048
MAX_DEVICE_NAME_LENGTH = 64
MAX_HOST_LENGTH = 253
_VALID_HOST = re.compile(r"^[A-Za-z0-9._-]+$")


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, value))


def _bounded_float(value, default, minimum, maximum):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not np.isfinite(value):
        return default
    return max(minimum, min(maximum, value))


def _safe_text(value, default, maximum):
    if not isinstance(value, str):
        return default
    value = value.strip()
    return value[:maximum] or default


def _valid_host(value):
    """Accept IPv4 addresses and ordinary DNS/mDNS names, never URLs/paths."""
    host = _safe_text(value, "", MAX_HOST_LENGTH)
    if not host or not _VALID_HOST.fullmatch(host):
        return ""
    if host.startswith((".", "-")) or host.endswith((".", "-")) or ".." in host:
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not (address.is_private or address.is_link_local or address.is_loopback):
            return ""
    return host


def _ease_in_out(t):
    """Cubic ease-in-out — same formula as easeInOut() in the kiosk JS."""
    if t < 0.5:
        return 4 * t * t * t
    x = -2 * t + 2
    return 1 - (x * x * x) / 2


# ── Config ────────────────────────────────────────────────────


def _normalize_devices(wled):
    """Return the configured device list as [{host, name, pixel_count}, ...].

    Accepts both shapes for backwards-compat with single-device configs:

      * New: ``wled.devices = [{"host": ..., "name": ..., "pixel_count": ...}]``
      * Legacy: ``wled.host = ...`` / ``wled.name = ...`` / ``wled.pixel_count = ...``

    Empty / missing → empty list.
    """
    if not isinstance(wled, dict):
        return []
    raw = wled.get("devices")
    out = []
    if isinstance(raw, list) and raw:
        seen_hosts = set()
        for entry in raw[:MAX_DEVICES]:
            if not isinstance(entry, dict):
                continue
            host = _valid_host(entry.get("host"))
            if not host or host.lower() in seen_hosts:
                continue
            seen_hosts.add(host.lower())
            out.append({
                "host": host,
                "name": _safe_text(entry.get("name"), host, MAX_DEVICE_NAME_LENGTH),
                "pixel_count": _bounded_int(
                    entry.get("pixel_count"), 46, 1, MAX_PIXELS_PER_DEVICE
                ),
                "reverse": entry.get("reverse") is True,
                "phase_offset": _bounded_float(
                    entry.get("phase_offset"), 0.0, -1.0, 1.0
                ) % 1.0,
                "brightness": _bounded_float(
                    entry.get("brightness"), 1.0, 0.05, 1.0
                ),
                "gamma": _bounded_float(entry.get("gamma"), 1.0, 0.5, 3.0),
            })
        return out

    # Legacy single-device fallback.
    legacy_host = _valid_host(wled.get("host"))
    if legacy_host:
        out.append({
            "host": legacy_host,
            "name": _safe_text(wled.get("name"), legacy_host, MAX_DEVICE_NAME_LENGTH),
            "pixel_count": _bounded_int(
                wled.get("pixel_count"), 46, 1, MAX_PIXELS_PER_DEVICE
            ),
            "reverse": wled.get("reverse") is True,
            "phase_offset": _bounded_float(
                wled.get("phase_offset"), 0.0, -1.0, 1.0
            ) % 1.0,
            "brightness": _bounded_float(
                wled.get("brightness"), 1.0, 0.05, 1.0
            ),
            "gamma": _bounded_float(wled.get("gamma"), 1.0, 0.5, 3.0),
        })
    return out


def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    wled = data.get("wled") or {}
    if not isinstance(wled, dict):
        wled = {}
    return {
        "enabled": wled.get("enabled") is True,
        "devices": _normalize_devices(wled),
        "palette_colors": _bounded_int(wled.get("palette_colors"), 3, 2, 8),
        "saturation_boost": _bounded_float(
            wled.get("saturation_boost"), 1.3, 0.25, 3.0
        ),
        # 1.8 s/rev = 33⅓ RPM — matches the vinyl record on the display.
        "gradient_drift_seconds": _bounded_float(
            wled.get("gradient_drift_seconds"), 1.8, 0.5, 60.0
        ),
        "dim_band_width": _bounded_int(wled.get("dim_band_width"), 3, 0, 256),
        "play_fps": _bounded_int(wled.get("play_fps"), 30, 1, 60),
        "pause_fps": _bounded_int(wled.get("pause_fps"), 1, 1, 30),
        # Explicit None check so the user can set this to 0 to disable
        # release-on-pause without `or` collapsing it to the default.
        "pause_release_seconds": _bounded_int(
            60 if wled.get("pause_release_seconds") is None else wled["pause_release_seconds"],
            60,
            0,
            86400,
        ),
        "realtime_timeout_seconds": _bounded_int(
            wled.get("realtime_timeout_seconds"), 2, 1, 255
        ),
    }


# ── Now-playing fetch ────────────────────────────────────────


@dataclass(frozen=True)
class PlaybackFetch:
    """Tri-state result: active playback, explicit idle, or fetch failure."""

    state: str
    snapshot: dict | None = None
    error: str | None = None


def fetch_now_playing():
    """Fetch and validate playback without conflating idle with failure."""
    try:
        resp = requests.get(NOW_PLAYING_URL, timeout=1.5)
    except requests.RequestException as exc:
        return PlaybackFetch("error", error=f"request failed: {exc}")
    if resp.status_code == 204:
        return PlaybackFetch("idle")
    if resp.status_code != 200:
        return PlaybackFetch("error", error=f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        return PlaybackFetch("error", error=f"invalid JSON: {exc}")

    if not isinstance(data, dict):
        return PlaybackFetch("error", error="response is not an object")
    item = data.get("item") or {}
    if not isinstance(item, dict):
        return PlaybackFetch("error", error="item is not an object")
    track_id = _safe_text(item.get("id"), "", 256)
    if not track_id:
        return PlaybackFetch("idle")
    album = item.get("album") or {}
    if not isinstance(album, dict):
        album = {}
    images = album.get("images") or []
    art_url = None
    if isinstance(images, list) and images and isinstance(images[0], dict):
        candidate = images[0].get("url")
        if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
            art_url = candidate[:2048]
    snap = {
        "is_playing": bool(data.get("is_playing")),
        "progress_ms": _bounded_int(data.get("progress_ms"), 0, 0, 86_400_000),
        "track_id": track_id,
        "duration_ms": _bounded_int(item.get("duration_ms"), 0, 0, 86_400_000),
        "art_url": art_url,
        "is_single": (album.get("album_type") or "") == "single",
        "monotonic_time": time.monotonic(),
    }
    return PlaybackFetch("active", snapshot=snap)


# ── Color extraction ─────────────────────────────────────────


def _boost_saturation(rgb, boost):
    r, g, b = (c / 255.0 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    s = min(1.0, s * boost)
    r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
    return (int(round(r2 * 255)), int(round(g2 * 255)), int(round(b2 * 255)))


def extract_palette(image_bytes, n, saturation_boost):
    """Return a list of n (r,g,b) tuples chosen for vivid LED appearance.

    Strategy:
      1. Quantize the artwork to a generous candidate pool.
      2. Score each candidate by (chroma × frequency_weight × value_weight),
         so we prefer punchy accent colors over big swathes of muddy
         background. Tiny but vivid accents (a neon sign in a dark photo)
         still win against a giant gray-brown sky.
      3. Push the resulting colors way up the saturation axis — LEDs need
         it. Always boost; never trust the original saturation directly.
      4. If the artwork is genuinely monochrome, synthesize a value-ramp
         from the dominant hue so the strip still has visible variation.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize((100, 100))
    quant = img.quantize(colors=32, method=Image.Quantize.MEDIANCUT)
    raw_palette = quant.getpalette() or []
    counts = quant.getcolors() or []
    total_pixels = sum(c for c, _ in counts) or 1

    candidates = []
    for count, idx in counts:
        base = idx * 3
        if base + 2 >= len(raw_palette):
            continue
        rgb = (raw_palette[base], raw_palette[base + 1], raw_palette[base + 2])
        r, g, b = (c / 255.0 for c in rgb)
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        chroma = max(r, g, b) - min(r, g, b)  # 0..1 — how non-gray the color is
        freq = count / total_pixels
        # Heavy chroma weight; value cap so near-black doesn't dominate even
        # if it's chromatic. sqrt(freq) damps the "huge background area" bias.
        score = (chroma ** 1.5 + 0.05) * (freq ** 0.4) * max(0.15, min(1.0, v))
        candidates.append((score, chroma, rgb, h, s, v))

    # Sort by score desc. Pick top, but enforce hue diversity so we don't
    # return three near-identical greens when the cover is a green forest.
    candidates.sort(reverse=True)
    chosen = []
    chosen_hues = []
    HUE_MIN_GAP = 0.08  # ~30°
    for score, chroma, rgb, h, s, v in candidates:
        if any(min(abs(h - hh), 1 - abs(h - hh)) < HUE_MIN_GAP for hh in chosen_hues):
            continue
        chosen.append((rgb, h, s, v, chroma))
        chosen_hues.append(h)
        if len(chosen) >= n:
            break
    # If hue-diversity filtering left us short, fall back to top-scoring.
    if len(chosen) < n:
        for score, chroma, rgb, h, s, v in candidates:
            if not any(rgb == c[0] for c in chosen):
                chosen.append((rgb, h, s, v, chroma))
            if len(chosen) >= n:
                break

    out = []
    for rgb, h, s, v in [(c[0], c[1], c[2], c[3]) for c in chosen[:n]]:
        # Floor saturation aggressively, then push higher. Floor at 0.55 so
        # even photos of beige sand come out punchy on the strip.
        new_s = min(1.0, max(s * saturation_boost, 0.55))
        # Floor brightness so dim colors don't disappear in the gradient
        # interpolation, but don't max it out (we still want a band of dim).
        new_v = max(0.55, v)
        nr, ng, nb = colorsys.hsv_to_rgb(h, new_s, new_v)
        out.append((int(round(nr * 255)), int(round(ng * 255)), int(round(nb * 255))))

    # Image was monochrome / hue-collapsed → synthesize a value ramp.
    if len(out) < n and out:
        base = out[0]
        h, s, v = colorsys.rgb_to_hsv(*(c / 255.0 for c in base))
        for i in range(n - len(out)):
            t = (i + 1) / (n - len(out) + 1)
            nv = 0.4 + 0.6 * t
            nr, ng, nb = colorsys.hsv_to_rgb(h, max(s, 0.7), nv)
            out.append((int(round(nr * 255)), int(round(ng * 255)), int(round(nb * 255))))

    while len(out) < n:
        out.append((128, 128, 128))
    return out[:n]


class PaletteWorker:
    """Async palette fetcher — keeps the main render loop unblocked on track change.

    Returns cached palettes synchronously; for cache misses, kicks off a
    background HTTP fetch + extraction and returns ``None`` until the result
    lands. The main loop polls each tick and starts a crossfade as soon as
    the new palette becomes available, so the strip never freezes while
    waiting on Spotify's CDN.
    """

    RETRY_BACKOFF_SECONDS = 30.0
    ERROR_LOG_INTERVAL_SECONDS = 300.0
    CACHE_LIMIT = 32
    ATTEMPT_LIMIT = 64

    def __init__(self):
        self._condition = threading.Condition()
        self._cache = OrderedDict()
        self._last_attempt = OrderedDict()
        self._last_error_log = OrderedDict()
        self._active_key = None
        self._pending = None
        self._stop = False
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="palette-worker"
        )
        self._thread.start()

    @staticmethod
    def cache_key(track_id, art_url, n, saturation_boost):
        return (track_id, art_url, n, round(saturation_boost, 3))

    def get_or_fetch(self, track_id, art_url, n, saturation_boost):
        key = self.cache_key(track_id, art_url, n, saturation_boost)
        now = time.monotonic()
        with self._condition:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            if not art_url or key == self._active_key:
                return None
            previous_attempt = self._last_attempt.get(key)
            if (
                previous_attempt is not None
                and now - previous_attempt < self.RETRY_BACKOFF_SECONDS
            ):
                return None
            self._last_attempt[key] = now
            self._last_attempt.move_to_end(key)
            while len(self._last_attempt) > self.ATTEMPT_LIMIT:
                self._last_attempt.popitem(last=False)
            # One pending slot means rapid skips replace obsolete queued art.
            self._pending = (key, art_url)
            self._condition.notify()
        return None

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._stop:
                    self._condition.wait()
                if self._stop:
                    return
                key, art_url = self._pending
                self._pending = None
                self._active_key = key
            self._fetch(key, art_url)
            with self._condition:
                self._active_key = None

    def _fetch(self, key, art_url):
        track_id, _art_url, n, saturation_boost = key
        try:
            resp = requests.get(art_url, timeout=5)
            if resp.status_code != 200:
                self._log_fetch_error(
                    key, f"art fetch HTTP {resp.status_code} for {track_id}"
                )
                return
            palette = extract_palette(resp.content, n, saturation_boost)
            with self._condition:
                self._cache[key] = palette
                self._cache.move_to_end(key)
                while len(self._cache) > self.CACHE_LIMIT:
                    self._cache.popitem(last=False)
            print(f"wled_sync: palette ready for {track_id}", flush=True)
        except (requests.RequestException, OSError, ValueError) as e:
            self._log_fetch_error(key, f"palette fetch failed for {track_id}: {e}")

    def _log_fetch_error(self, key, message):
        now = time.monotonic()
        with self._condition:
            previous = self._last_error_log.get(key, 0.0)
            if previous and now - previous < self.ERROR_LOG_INTERVAL_SECONDS:
                return
            self._last_error_log[key] = now
            self._last_error_log.move_to_end(key)
            while len(self._last_error_log) > self.ATTEMPT_LIMIT:
                self._last_error_log.popitem(last=False)
        print(f"wled_sync: {message}", flush=True)

    def stop(self):
        with self._condition:
            self._stop = True
            self._condition.notify()


# ── Frame rendering ──────────────────────────────────────────


def _lerp_color(a, b, t):
    return (
        int(round(a[0] + (b[0] - a[0]) * t)),
        int(round(a[1] + (b[1] - a[1]) * t)),
        int(round(a[2] + (b[2] - a[2]) * t)),
    )


def _crossfade_palettes(old, new, t):
    if old is None:
        return new
    if new is None:
        return old
    if t >= 1.0:
        return new
    length = max(len(old), len(new))
    out = []
    for i in range(length):
        a = old[i % len(old)]
        b = new[i % len(new)]
        out.append(_lerp_color(a, b, t))
    return out


def _start_palette_crossfade(current, target, started_at, new_target, now):
    """Start from the palette visibly rendered now, never the pre-fade source."""
    if current is None or target is None:
        return new_target, new_target, 0.0
    progress = min(1.0, max(0.0, (now - started_at) / CROSSFADE_SECONDS)) \
        if started_at else 1.0
    visible = _crossfade_palettes(current, target, progress)
    return visible, new_target, now


def _render_fps(config, is_playing, spin_speed, spin_target, crossfade_t):
    settling = spin_speed != spin_target or crossfade_t < 1.0
    return config["play_fps"] if is_playing or settling else config["pause_fps"]


def _complement_rgb(rgb):
    """Return the hue-opposite of an RGB tuple, forced to full brightness."""
    r, g, b = (c / 255.0 for c in rgb)
    h, _s, _v = colorsys.rgb_to_hsv(r, g, b)
    h2 = (h + 0.5) % 1.0
    nr, ng, nb = colorsys.hsv_to_rgb(h2, 1.0, 1.0)
    return (int(round(nr * 255)), int(round(ng * 255)), int(round(nb * 255)))


def _palette_complement(palette):
    """Per-palette-color complement as a (n, 3) float32 array.

    Cheap: palette is tiny (default 3 colors). Pre-computing once per frame
    lets us avoid per-pixel HSV math inside ``build_frame``.
    """
    out = np.empty((len(palette), 3), dtype=np.float32)
    for i, rgb in enumerate(palette):
        out[i] = _complement_rgb(rgb)
    return out


def build_frame(
    palette,
    pixel_count,
    phase,
    progress_frac,
    band_width,
    _unused_dim_value=None,
    *,
    reverse=False,
    brightness=1.0,
    gamma=1.0,
):
    """Build a (pixel_count * 3) byte buffer.

    Base layer: palette gradient interpolated across the strip, slowly drifting
    with `phase` in [0, 1).

    Progress band: a `band_width`-wide region whose float center moves
    continuously across [0, pixel_count-1] as `progress_frac` goes 0 → 1.
    Each pixel is rendered as a linear blend between the underlying gradient
    color and the *palette complement* gradient (full brightness + saturation),
    weighted by the pixel's overlap with the band's continuous interval.
    Subpixel coverage = smooth perceived motion even between integer positions.

    Note: the complement gradient is built by interpolating per-palette
    complements (rather than complementing the interpolated gradient color
    per pixel). For the small palettes used here the visual result is
    indistinguishable from the per-pixel version, and the numpy vectorisation
    keeps the render cheap enough for 30+ FPS on a Pi.
    """
    if pixel_count <= 0:
        return b""
    n = len(palette)
    palette_arr = np.asarray(palette, dtype=np.float32)
    complement_arr = _palette_complement(palette)

    if pixel_count > 1:
        t = np.arange(pixel_count, dtype=np.float32) / (pixel_count - 1)
    else:
        t = np.zeros(1, dtype=np.float32)
    pos = ((t + phase) % 1.0) * n
    idx = np.floor(pos).astype(np.int64)
    frac = (pos - idx)[:, None]

    a = palette_arr[idx % n]
    b = palette_arr[(idx + 1) % n]
    grad = a + (b - a) * frac

    a_c = complement_arr[idx % n]
    b_c = complement_arr[(idx + 1) % n]
    grad_comp = a_c + (b_c - a_c) * frac

    coverage = np.zeros(pixel_count, dtype=np.float32)
    if band_width > 0 and pixel_count > band_width:
        travel = pixel_count - band_width
        band_start = max(0.0, min(travel, progress_frac * travel))
        band_end = band_start + band_width
        left = np.arange(pixel_count, dtype=np.float32)
        overlap = np.minimum(left + 1.0, band_end) - np.maximum(left, band_start)
        coverage = np.clip(overlap, 0.0, 1.0)

    cov = coverage[:, None]
    out = grad * (1.0 - cov) + grad_comp * cov
    if gamma != 1.0:
        out = 255.0 * np.power(np.clip(out / 255.0, 0.0, 1.0), gamma)
    if brightness != 1.0:
        out *= brightness
    if reverse:
        out = out[::-1]
    return np.clip(np.rint(out), 0, 255).astype(np.uint8).tobytes()


# ── UDP sender ───────────────────────────────────────────────


class WledSender:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._warn_log = {}  # host -> last_warn_time
        self._sent_count = 0
        self._failed_count = 0
        self._first_sent_hosts = set()
        self._last_log_count = 0
        self._last_log_time = 0.0
        self._log_interval = _bounded_float(
            os.environ.get("WLED_STATS_LOG_SECONDS"), 300.0, 30.0, 3600.0
        )

    def send(self, host, frame, timeout_seconds):
        if not host:
            return False
        packet = struct.pack("BB", DRGB_PROTOCOL_ID, timeout_seconds) + frame
        try:
            self.sock.sendto(packet, (host, WLED_UDP_PORT))
            self._sent_count += 1
            now = time.monotonic()
            if host not in self._first_sent_hosts:
                print(f"wled_sync: first DRGB packet sent ({len(packet)} bytes) to {host}:{WLED_UDP_PORT}", flush=True)
                self._first_sent_hosts.add(host)
                if self._last_log_time == 0.0:
                    self._last_log_time = now
                    self._last_log_count = self._sent_count
            elif now - self._last_log_time >= self._log_interval:
                window = now - self._last_log_time
                delta = self._sent_count - self._last_log_count
                print(
                    f"wled_sync: local UDP send summary: {delta} datagrams queued "
                    f"in {window:.0f}s ({self._failed_count} local send errors total); "
                    "UDP delivery is not acknowledged by WLED",
                    flush=True,
                )
                self._last_log_time = now
                self._last_log_count = self._sent_count
            return True
        except OSError as e:
            self._failed_count += 1
            now = time.monotonic()
            last_warn = self._warn_log.get(host, 0.0)
            if not last_warn or now - last_warn > 30:
                print(f"wled_sync: send to {host}:{WLED_UDP_PORT} failed: {e}", flush=True)
                self._warn_log[host] = now
            return False

    def diagnostics(self):
        return {
            "udp_datagrams_queued": self._sent_count,
            "udp_local_send_errors": self._failed_count,
            "hosts_seen": sorted(self._first_sent_hosts),
        }


# ── Snapshot tracking ────────────────────────────────────────


class PlaybackTracker:
    """Keep a last-good snapshot through brief API failures, but not true idle."""

    def __init__(self, failure_grace_seconds=PLAYBACK_FAILURE_GRACE_SECONDS):
        self._lock = threading.Lock()
        self._snapshot = None
        self._state = "starting"
        self._last_error = None
        self._consecutive_failures = 0
        self._last_poll_at = None
        self._last_success_at = None
        self._failure_grace_seconds = max(0.0, failure_grace_seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="playback-tracker"
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self):
        previous_state = None
        while not self._stop.is_set():
            try:
                result = fetch_now_playing()
            except Exception as exc:
                # A malformed response must never kill the tracker thread.
                result = PlaybackFetch("error", error=f"unexpected fetch error: {exc}")
            now = time.monotonic()
            state, error = self._apply_result(result, now)
            if state != previous_state:
                suffix = f" ({error})" if error else ""
                print(f"wled_sync: playback tracker state={state}{suffix}", flush=True)
                previous_state = state
            self._stop.wait(NOW_PLAYING_POLL_SECONDS)

    def _apply_result(self, result, now=None):
        """Apply one poll result; split out for deterministic state-machine tests."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._last_poll_at = now
            if result.state == "active":
                self._snapshot = result.snapshot
                self._state = "active"
                self._last_error = None
                self._consecutive_failures = 0
                self._last_success_at = now
            elif result.state == "idle":
                self._snapshot = None
                self._state = "idle"
                self._last_error = None
                self._consecutive_failures = 0
                self._last_success_at = now
            else:
                self._consecutive_failures += 1
                self._last_error = (
                    result.error or "unknown playback fetch failure"
                )[:256]
                success_age = (
                    now - self._last_success_at
                    if self._last_success_at is not None
                    else float("inf")
                )
                if success_age > self._failure_grace_seconds:
                    self._snapshot = None
                    self._state = "unavailable"
                else:
                    self._state = "degraded"
            return self._state, self._last_error

    def latest(self):
        with self._lock:
            return self._snapshot

    def diagnostics(self):
        now = time.monotonic()
        with self._lock:
            return {
                "state": self._state,
                "thread_alive": self._thread.is_alive(),
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
                "last_poll_age_seconds": (
                    round(now - self._last_poll_at, 3)
                    if self._last_poll_at is not None
                    else None
                ),
                "last_success_age_seconds": (
                    round(now - self._last_success_at, 3)
                    if self._last_success_at is not None
                    else None
                ),
                "failure_grace_seconds": self._failure_grace_seconds,
            }


def effective_progress_ms(snap):
    if not snap:
        return 0
    base = snap["progress_ms"]
    if snap["is_playing"]:
        base += int((time.monotonic() - snap["monotonic_time"]) * 1000)
    return base


def _write_status(payload):
    """Publish bounded machine-readable health without unsafe shared temp names."""
    if not STATUS_FILE:
        return
    directory = os.path.dirname(STATUS_FILE)
    try:
        os.makedirs(directory, mode=0o770, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".wled-status-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o640)
            os.replace(tmp_path, STATUS_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError:
        # /run may not exist in local development. Journald still has state
        # transition logs, so status-file failure is deliberately non-fatal.
        return


# ── Main loop ────────────────────────────────────────────────


def main():
    print("wled_sync: starting", flush=True)
    config = load_config()
    if not config["enabled"] or not config["devices"]:
        print("wled_sync: disabled or no valid devices; exiting until config changes", flush=True)
        return
    sender = WledSender()
    tracker = PlaybackTracker()
    tracker.start()
    palette_worker = PaletteWorker()

    config_loaded_at = time.monotonic()
    config_signature = None

    current_palette = None  # active palette in use
    target_palette = None   # palette we are crossfading toward
    target_palette_key = None
    crossfade_start = 0.0

    last_send = 0.0
    last_status_write = 0.0
    paused_since = None
    released_during_pause = False  # currently in "long pause = release" state

    # Vinyl-mirroring spin state — phase is a running float in [0, 1) that
    # advances at spin_speed * elapsed_seconds / drift_period each tick.
    # spin_speed eases between 0 and 1 over SPIN_RAMP_SECONDS, matching the
    # spinning record on the display exactly.
    phase = 0.0
    phase_updated_at = time.monotonic()
    spin_speed = 0.0
    spin_target = 0
    spin_start_time = 0.0
    spin_start_speed = 0.0

    # 45 Mode — singles rotate at 45 RPM instead of 33⅓. The factor eases
    # toward its target (matching the kiosk's platter-motor ramp) so a
    # mid-set album→single change doesn't snap the gradient speed.
    RPM_SINGLE_FACTOR = 45.0 / (100.0 / 3.0)  # = 1.35
    RPM_EASE_SECONDS = 0.45
    rpm_factor = 1.0

    try:
        while True:
            now = time.monotonic()

            if now - config_loaded_at >= CONFIG_RELOAD_SECONDS:
                config = load_config()
                config_loaded_at = now

            new_signature = json.dumps(config, sort_keys=True, separators=(",", ":"))
            if new_signature != config_signature:
                config_signature = new_signature
                if not config["enabled"]:
                    print("wled_sync: disabled in config; idling", flush=True)
                elif not config["devices"]:
                    print("wled_sync: enabled but no valid devices configured", flush=True)
                else:
                    device_summary = ", ".join(
                        f"{d['name']}@{d['host']} ({d['pixel_count']}px)"
                        for d in config["devices"]
                    )
                    print(
                        f"wled_sync: config loaded: {device_summary}; "
                        f"{config['play_fps']} play FPS/{config['pause_fps']} pause FPS",
                        flush=True,
                    )

            if now - last_status_write >= 10.0:
                status = {
                    "schema_version": 1,
                    "updated_unix": int(time.time()),
                    "enabled": config["enabled"],
                    "configured_devices": len(config["devices"]),
                    "rendering": bool(last_send and now - last_send < 5.0),
                    "spin_speed": round(spin_speed, 4),
                    "playback": tracker.diagnostics(),
                }
                status.update(sender.diagnostics())
                _write_status(status)
                last_status_write = now

            if not config["enabled"] or not config["devices"]:
                print("wled_sync: config disabled/emptied; releasing WLED and exiting", flush=True)
                return

            snap = tracker.latest()
            if not snap or not snap.get("track_id"):
                # Explicit idle, or failure beyond the grace period. Reset all
                # motor integration timestamps so the next track cannot jump.
                phase = 0.0
                phase_updated_at = now
                spin_speed = 0.0
                spin_target = 0
                spin_start_time = now
                spin_start_speed = 0.0
                rpm_factor = 1.0
                paused_since = None
                released_during_pause = False
                last_send = 0.0
                time.sleep(0.5)
                continue

            requested_palette_key = palette_worker.cache_key(
                snap["track_id"],
                snap.get("art_url"),
                config["palette_colors"],
                config["saturation_boost"],
            )
            if requested_palette_key != target_palette_key:
                new_palette = palette_worker.get_or_fetch(
                    snap["track_id"],
                    snap.get("art_url"),
                    config["palette_colors"],
                    config["saturation_boost"],
                )
                if new_palette:
                    # If a third track/config arrives mid-crossfade, begin the
                    # new fade from the color actually on the LEDs now.
                    current_palette, target_palette, crossfade_start = (
                        _start_palette_crossfade(
                            current_palette,
                            target_palette,
                            crossfade_start,
                            new_palette,
                            now,
                        )
                    )
                    target_palette_key = requested_palette_key

            if target_palette is None or current_palette is None:
                time.sleep(0.2)
                continue

            crossfade_t = (
                min(1.0, (now - crossfade_start) / CROSSFADE_SECONDS)
                if crossfade_start
                else 1.0
            )
            rendered_palette = _crossfade_palettes(
                current_palette, target_palette, crossfade_t
            )
            if crossfade_t >= 1.0:
                current_palette = target_palette
                crossfade_start = 0.0

            is_playing = bool(snap["is_playing"])
            if is_playing:
                if paused_since is not None and released_during_pause:
                    print("wled_sync: playback resumed; re-engaging WLED", flush=True)
                paused_since = None
                released_during_pause = False
            elif paused_since is None:
                paused_since = now

            new_target = 1 if is_playing else 0
            if new_target != spin_target:
                spin_target = new_target
                spin_start_time = now
                spin_start_speed = spin_speed
            if spin_speed != spin_target:
                spin_t = min((now - spin_start_time) / SPIN_RAMP_SECONDS, 1.0)
                spin_speed = spin_start_speed + (
                    spin_target - spin_start_speed
                ) * _ease_in_out(spin_t)
                if spin_t >= 1.0:
                    spin_speed = float(spin_target)

            drift_period = config["gradient_drift_seconds"]
            dt = max(0.0, now - phase_updated_at)
            rpm_target = RPM_SINGLE_FACTOR if snap.get("is_single") else 1.0
            if rpm_factor != rpm_target:
                rpm_factor += (rpm_target - rpm_factor) * min(
                    1.0, dt / RPM_EASE_SECONDS
                )
                if abs(rpm_factor - rpm_target) < 0.001:
                    rpm_factor = rpm_target
            phase = (phase + spin_speed * rpm_factor * dt / drift_period) % 1.0
            phase_updated_at = now

            pause_release = config["pause_release_seconds"]
            if (
                not is_playing
                and paused_since is not None
                and pause_release > 0
                and now - paused_since >= pause_release
            ):
                if not released_during_pause:
                    print(
                        f"wled_sync: paused for {now - paused_since:.0f}s; releasing WLED",
                        flush=True,
                    )
                    released_during_pause = True
                time.sleep(0.5)
                continue

            # Keep the high cadence throughout the motor ramp and palette fade;
            # switching to 1 FPS immediately on pause caused a four-frame stop.
            fps = _render_fps(
                config, is_playing, spin_speed, spin_target, crossfade_t
            )
            interval = 1.0 / fps
            if now - last_send < interval:
                time.sleep(max(0.0, interval - (now - last_send)))
                continue

            duration_ms = max(1, snap["duration_ms"])
            progress_frac = max(
                0.0, min(1.0, effective_progress_ms(snap) / duration_ms)
            )

            # Reuse frames for devices with identical rendering parameters.
            frame_cache = {}
            for device in config["devices"]:
                frame_key = (
                    device["pixel_count"],
                    device["reverse"],
                    device["phase_offset"],
                    device["brightness"],
                    device["gamma"],
                )
                frame = frame_cache.get(frame_key)
                if frame is None:
                    frame = build_frame(
                        rendered_palette,
                        device["pixel_count"],
                        (phase + device["phase_offset"]) % 1.0,
                        progress_frac,
                        config["dim_band_width"],
                        reverse=device["reverse"],
                        brightness=device["brightness"],
                        gamma=device["gamma"],
                    )
                    frame_cache[frame_key] = frame
                sender.send(
                    device["host"], frame, config["realtime_timeout_seconds"]
                )
            last_send = now
    finally:
        tracker.stop()
        palette_worker.stop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("wled_sync: stopped")
