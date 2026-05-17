#!/usr/bin/env python3
"""WLED ambient sync — mirror album-art colors and track progress on a WLED strip.

While Spotify is playing, this service streams a full per-pixel buffer to WLED
over UDP DRGB at a few Hz. The buffer is a gradient interpolated across the
extracted album-art palette, with a small "dim band" whose position along the
strip reflects how far through the track we are.

When playback is paused, the same gradient keeps drifting at a slower cadence
but the dim band freezes. When truly idle (no track, or Spotify disconnected),
this service stops sending packets entirely — WLED's realtime mode times out
after `realtime_timeout_seconds` and reverts to whatever the user configured
on the device itself.

Configuration is re-read from `config.json` every loop tick, so changes made
via the kiosk's WLED setup UI take effect on the next render without a service
restart.
"""

import colorsys
import io
import json
import os
import socket
import struct
import sys
import threading
import time

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


# ── Config ────────────────────────────────────────────────────


def _normalize_devices(wled):
    """Return the configured device list as [{host, name, pixel_count}, ...].

    Accepts both shapes for backwards-compat with single-device configs:

      * New: ``wled.devices = [{"host": ..., "name": ..., "pixel_count": ...}]``
      * Legacy: ``wled.host = ...`` / ``wled.name = ...`` / ``wled.pixel_count = ...``

    Empty / missing → empty list.
    """
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

    # Legacy single-device fallback.
    legacy_host = (wled.get("host") or "").strip()
    if legacy_host:
        out.append({
            "host": legacy_host,
            "name": (wled.get("name") or legacy_host).strip(),
            "pixel_count": max(1, int(wled.get("pixel_count") or 46)),
        })
    return out


def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    wled = data.get("wled") or {}
    return {
        "enabled": bool(wled.get("enabled", False)),
        "devices": _normalize_devices(wled),
        "palette_colors": max(2, int(wled.get("palette_colors") or 3)),
        "saturation_boost": float(wled.get("saturation_boost") or 1.3),
        "gradient_drift_seconds": float(wled.get("gradient_drift_seconds") or 20.0),
        "dim_band_width": max(0, int(wled.get("dim_band_width") or 3)),
        "play_fps": max(1, int(wled.get("play_fps") or 5)),
        "pause_fps": max(1, int(wled.get("pause_fps") or 1)),
        # Explicit None check so the user can set this to 0 to disable
        # release-on-pause without `or` collapsing it to the default.
        "pause_release_seconds": max(
            0,
            int(60 if wled.get("pause_release_seconds") is None else wled["pause_release_seconds"]),
        ),
        "realtime_timeout_seconds": max(
            1, min(255, int(wled.get("realtime_timeout_seconds") or 2))
        ),
    }


# ── Now-playing fetch ────────────────────────────────────────


def fetch_now_playing():
    """Return a snapshot dict or None if nothing is playing / unreachable."""
    try:
        resp = requests.get(NOW_PLAYING_URL, timeout=1.5)
    except requests.RequestException:
        return None
    if resp.status_code == 204:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    item = data.get("item") or {}
    album = item.get("album") or {}
    images = album.get("images") or []
    art_url = images[0].get("url") if images else None
    return {
        "is_playing": bool(data.get("is_playing")),
        "progress_ms": int(data.get("progress_ms") or 0),
        "track_id": item.get("id"),
        "duration_ms": int(item.get("duration_ms") or 0),
        "art_url": art_url,
        "wall_time": time.time(),
    }


# ── Color extraction ─────────────────────────────────────────

_palette_cache = {}


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


def get_palette_for(track_id, art_url, n, saturation_boost):
    cache_key = (track_id, n, round(saturation_boost, 2))
    if cache_key in _palette_cache:
        return _palette_cache[cache_key]
    if not art_url:
        return None
    try:
        resp = requests.get(art_url, timeout=5)
        if resp.status_code != 200:
            return None
        palette = extract_palette(resp.content, n, saturation_boost)
    except (requests.RequestException, OSError, ValueError) as e:
        print(f"wled_sync: palette extraction failed for {track_id}: {e}")
        return None
    _palette_cache[cache_key] = palette
    return palette


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


def _gradient_color(palette, position):
    """Sample a cyclical gradient through palette at position in [0, 1)."""
    n = len(palette)
    scaled = (position % 1.0) * n
    idx = int(scaled)
    frac = scaled - idx
    a = palette[idx % n]
    b = palette[(idx + 1) % n]
    return _lerp_color(a, b, frac)


def _complement_rgb(rgb):
    """Return the hue-opposite of an RGB tuple, forced to full brightness."""
    r, g, b = (c / 255.0 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h2 = (h + 0.5) % 1.0
    nr, ng, nb = colorsys.hsv_to_rgb(h2, 1.0, 1.0)
    return (int(round(nr * 255)), int(round(ng * 255)), int(round(nb * 255)))


def build_frame(palette, pixel_count, phase, progress_frac, band_width, _unused_dim_value=None):
    """Build a (pixel_count * 3) byte buffer.

    Base layer: palette gradient interpolated across the strip, slowly drifting
    with `phase` in [0, 1).

    Progress band: a `band_width`-wide region whose float center moves
    continuously across [0, pixel_count-1] as `progress_frac` goes 0 → 1.
    Each pixel is rendered as a linear blend between the underlying gradient
    color and that gradient color's *hue-opposite* (full brightness + saturation),
    weighted by the pixel's overlap with the band's continuous interval.
    Subpixel coverage = smooth perceived motion even between integer positions.
    """
    buf = bytearray(pixel_count * 3)
    # Compute gradient first.
    grad = [(0, 0, 0)] * pixel_count
    for i in range(pixel_count):
        t = (i / max(pixel_count - 1, 1)) + phase
        grad[i] = _gradient_color(palette, t)

    # Band interval, in pixel coordinates where pixel i occupies [i, i+1].
    if band_width > 0 and pixel_count > band_width:
        travel = pixel_count - band_width
        band_start = max(0.0, min(travel, progress_frac * travel))
        band_end = band_start + band_width
    else:
        band_start = band_end = -1.0  # no band

    for i in range(pixel_count):
        base = grad[i]
        if band_end > 0:
            pixel_left = float(i)
            pixel_right = float(i + 1)
            overlap = min(pixel_right, band_end) - max(pixel_left, band_start)
            coverage = 0.0 if overlap <= 0 else (overlap if overlap < 1 else 1.0)
        else:
            coverage = 0.0
        if coverage > 0:
            comp = _complement_rgb(base)
            r = int(round(base[0] * (1 - coverage) + comp[0] * coverage))
            g = int(round(base[1] * (1 - coverage) + comp[1] * coverage))
            b = int(round(base[2] * (1 - coverage) + comp[2] * coverage))
        else:
            r, g, b = base
        buf[i * 3 + 0] = r
        buf[i * 3 + 1] = g
        buf[i * 3 + 2] = b
    return bytes(buf)


# ── UDP sender ───────────────────────────────────────────────


class WledSender:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._warn_log = {}  # host -> last_warn_time
        self._sent_count = 0
        self._first_sent_hosts = set()
        self._last_log_count = 0
        self._last_log_time = 0.0

    def send(self, host, frame, timeout_seconds):
        if not host:
            return False
        packet = struct.pack("BB", DRGB_PROTOCOL_ID, timeout_seconds) + frame
        try:
            self.sock.sendto(packet, (host, WLED_UDP_PORT))
            self._sent_count += 1
            now = time.time()
            if host not in self._first_sent_hosts:
                print(f"wled_sync: first DRGB packet sent ({len(packet)} bytes) to {host}:{WLED_UDP_PORT}", flush=True)
                self._first_sent_hosts.add(host)
                if self._last_log_time == 0.0:
                    self._last_log_time = now
                    self._last_log_count = self._sent_count
            elif now - self._last_log_time > 30:
                delta = self._sent_count - self._last_log_count
                print(f"wled_sync: sent {delta} packets in last {now - self._last_log_time:.1f}s "
                      f"(total {self._sent_count}, hosts={len(self._first_sent_hosts)})", flush=True)
                self._last_log_time = now
                self._last_log_count = self._sent_count
            return True
        except OSError as e:
            now = time.time()
            last_warn = self._warn_log.get(host, 0.0)
            if now - last_warn > 30:
                print(f"wled_sync: send to {host}:{WLED_UDP_PORT} failed: {e}", flush=True)
                self._warn_log[host] = now
            return False


# ── Snapshot tracking ────────────────────────────────────────


class PlaybackTracker:
    """Polls /api/now-playing on a background thread and exposes the latest snapshot."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            snap = fetch_now_playing()
            with self._lock:
                self._snapshot = snap
            self._stop.wait(NOW_PLAYING_POLL_SECONDS)

    def latest(self):
        with self._lock:
            return self._snapshot


def effective_progress_ms(snap):
    if not snap:
        return 0
    base = snap["progress_ms"]
    if snap["is_playing"]:
        base += int((time.time() - snap["wall_time"]) * 1000)
    return base


# ── Main loop ────────────────────────────────────────────────


def main():
    print("wled_sync: starting")
    sender = WledSender()
    tracker = PlaybackTracker()
    tracker.start()

    config = load_config()
    config_loaded_at = time.time()

    if not config["enabled"]:
        print("wled_sync: disabled in config (wled.enabled=false). Idling.")
    elif not config["devices"]:
        print("wled_sync: enabled but no devices configured. Waiting for setup via kiosk UI.")
    else:
        print(f"wled_sync: enabled with {len(config['devices'])} device(s): "
              + ", ".join(f"{d['name']}@{d['host']} ({d['pixel_count']}px)" for d in config["devices"]))

    current_palette = None  # active palette in use
    target_palette = None   # palette we are crossfading toward
    target_track_id = None
    crossfade_start = 0.0

    last_send = 0.0
    started_at = time.time()
    paused_since = None     # wall time when is_playing first observed False
    released_during_pause = False  # currently in "long pause = release" state

    while True:
        now = time.time()

        if now - config_loaded_at >= CONFIG_RELOAD_SECONDS:
            config = load_config()
            config_loaded_at = now

        if not config["enabled"] or not config["devices"]:
            time.sleep(1.0)
            continue

        snap = tracker.latest()
        if not snap or not snap.get("track_id"):
            # True idle — stop sending so WLED's realtime mode times out
            # and the device reverts to whatever the user has configured.
            time.sleep(0.5)
            continue

        # Palette: re-extract on track change, then crossfade.
        if snap["track_id"] != target_track_id:
            print(f"wled_sync: new track {snap['track_id']} — extracting palette", flush=True)
            new_palette = get_palette_for(
                snap["track_id"],
                snap.get("art_url"),
                config["palette_colors"],
                config["saturation_boost"],
            )
            if new_palette:
                print(f"wled_sync: palette {new_palette}", flush=True)
                target_palette = new_palette
                target_track_id = snap["track_id"]
                crossfade_start = now
                if current_palette is None:
                    current_palette = target_palette
            else:
                print(f"wled_sync: palette extraction returned None for {snap['track_id']} (art_url={snap.get('art_url')})", flush=True)

        if target_palette is None or current_palette is None:
            time.sleep(0.2)
            continue

        # Crossfade current → target
        t = min(1.0, (now - crossfade_start) / CROSSFADE_SECONDS) if crossfade_start else 1.0
        rendered_palette = _crossfade_palettes(current_palette, target_palette, t)
        if t >= 1.0:
            current_palette = target_palette

        # Pause tracking: record when pause began, clear when playback resumes.
        is_playing = bool(snap["is_playing"])
        if is_playing:
            if paused_since is not None:
                if released_during_pause:
                    print("wled_sync: playback resumed — re-engaging WLED", flush=True)
                paused_since = None
                released_during_pause = False
        elif paused_since is None:
            paused_since = now

        # Long pause = treat as idle: stop sending so WLED's realtime mode
        # times out and the device reverts to whatever preset is configured
        # locally. `pause_release_seconds = 0` disables this behaviour and
        # restores the legacy "drive indefinitely while paused" mode.
        pause_release = config["pause_release_seconds"]
        if not is_playing and paused_since is not None and pause_release > 0:
            if now - paused_since >= pause_release:
                if not released_during_pause:
                    print(
                        f"wled_sync: paused for {now - paused_since:.0f}s — releasing WLED",
                        flush=True,
                    )
                    released_during_pause = True
                time.sleep(0.5)
                continue

        # Pick cadence and decide whether it's time to send.
        fps = config["play_fps"] if is_playing else config["pause_fps"]
        interval = 1.0 / fps
        if now - last_send < interval:
            # Sleep a short slice so we stay responsive to config/track changes.
            time.sleep(min(0.05, max(0.0, interval - (now - last_send))))
            continue

        # Progress fraction for the dim band — only advances while playing.
        duration_ms = max(1, snap["duration_ms"])
        progress_ms = effective_progress_ms(snap)
        progress_frac = max(0.0, min(1.0, progress_ms / duration_ms))

        # Gradient drift phase — gives the ambient look even on long pauses.
        drift_period = max(0.5, config["gradient_drift_seconds"])
        phase = ((now - started_at) % drift_period) / drift_period

        # Render and send to every configured device. Each strip gets the same
        # palette + phase + progress fraction, scaled to its own pixel count,
        # so a 30-LED strip and a 100-LED strip stay visually in sync as one
        # piece of "house lighting."
        for device in config["devices"]:
            frame = build_frame(
                rendered_palette,
                device["pixel_count"],
                phase,
                progress_frac,
                config["dim_band_width"],
            )
            sender.send(device["host"], frame, config["realtime_timeout_seconds"])
        last_send = now


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("wled_sync: stopped")
