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
from concurrent.futures import ThreadPoolExecutor

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
        # 1.8 s/rev = 33⅓ RPM — matches the vinyl record on the display.
        "gradient_drift_seconds": float(wled.get("gradient_drift_seconds") or 1.8),
        "dim_band_width": max(0, int(wled.get("dim_band_width") or 3)),
        "play_fps": max(1, int(wled.get("play_fps") or 30)),
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
        # 45 Mode: singles rotate the gradient at 45 RPM, matching the kiosk.
        "is_single": (album.get("album_type") or "") == "single",
        "wall_time": time.time(),
    }


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

    RETRY_BACKOFF_SECONDS = 5.0  # min gap between fetches for the same key

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}              # cache_key -> palette
        self._inflight = set()        # cache_keys currently being fetched
        self._last_attempt = {}       # cache_key -> wall time of last fetch
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="palette")

    def get_or_fetch(self, track_id, art_url, n, saturation_boost):
        key = (track_id, n, round(saturation_boost, 2))
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            if key in self._inflight or not art_url:
                return None
            if now - self._last_attempt.get(key, 0.0) < self.RETRY_BACKOFF_SECONDS:
                return None
            self._inflight.add(key)
            self._last_attempt[key] = now
        self._executor.submit(self._fetch, key, art_url)
        return None

    def _fetch(self, key, art_url):
        track_id, n, saturation_boost = key
        try:
            resp = requests.get(art_url, timeout=5)
            if resp.status_code != 200:
                print(f"wled_sync: art fetch HTTP {resp.status_code} for {track_id}", flush=True)
                return
            palette = extract_palette(resp.content, n, saturation_boost)
            with self._lock:
                self._cache[key] = palette
            print(f"wled_sync: palette for {track_id} = {palette}", flush=True)
        except (requests.RequestException, OSError, ValueError) as e:
            print(f"wled_sync: palette fetch failed for {track_id}: {e}", flush=True)
        finally:
            with self._lock:
                self._inflight.discard(key)


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


def build_frame(palette, pixel_count, phase, progress_frac, band_width, _unused_dim_value=None):
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
    return np.clip(np.rint(out), 0, 255).astype(np.uint8).tobytes()


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
            now = time.monotonic()
            if host not in self._first_sent_hosts:
                print(f"wled_sync: first DRGB packet sent ({len(packet)} bytes) to {host}:{WLED_UDP_PORT}", flush=True)
                self._first_sent_hosts.add(host)
                if self._last_log_time == 0.0:
                    self._last_log_time = now
                    self._last_log_count = self._sent_count
            elif now - self._last_log_time >= 10:
                window = now - self._last_log_time
                delta = self._sent_count - self._last_log_count
                hosts = max(1, len(self._first_sent_hosts))
                # delta counts packets across all hosts — divide by host count
                # to get the per-host frame rate (which is what the user sees).
                fps = delta / window / hosts
                print(
                    f"wled_sync: {fps:.1f} FPS per host "
                    f"({delta} packets to {hosts} host(s) in {window:.1f}s, total {self._sent_count})",
                    flush=True,
                )
                self._last_log_time = now
                self._last_log_count = self._sent_count
            return True
        except OSError as e:
            now = time.monotonic()
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
    palette_worker = PaletteWorker()

    config = load_config()
    config_loaded_at = time.monotonic()

    if not config["enabled"]:
        print("wled_sync: disabled in config (wled.enabled=false). Idling.")
    elif not config["devices"]:
        print("wled_sync: enabled but no devices configured. Waiting for setup via kiosk UI.")
    else:
        print(f"wled_sync: enabled with {len(config['devices'])} device(s): "
              + ", ".join(f"{d['name']}@{d['host']} ({d['pixel_count']}px)" for d in config["devices"]))
        # Surface the effective render cadence so it's obvious from the logs
        # whether the configured FPS matches the user's expectation.
        max_pixels = max((d["pixel_count"] for d in config["devices"]), default=0)
        recommended = max(1, int(round(max_pixels / max(0.5, config["gradient_drift_seconds"]))))
        print(
            f"wled_sync: target {config['play_fps']} FPS while playing "
            f"({1000.0 / config['play_fps']:.1f} ms/frame), "
            f"{config['pause_fps']} FPS while paused. "
            f"Smooth-drift threshold for longest strip ({max_pixels} px) is "
            f"~{recommended} FPS.",
            flush=True,
        )

    current_palette = None  # active palette in use
    target_palette = None   # palette we are crossfading toward
    target_track_id = None
    crossfade_start = 0.0

    last_send = 0.0
    paused_since = None     # wall time when is_playing first observed False
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

    while True:
        # Monotonic clock for everything render-timing-related: it can't jump
        # backward on NTP corrections, which would otherwise stall the loop
        # for whatever the jump distance was. Wall clock (time.time()) is
        # still used in fetch_now_playing/effective_progress_ms — those need
        # to stay aligned with Spotify's reported progress.
        now = time.monotonic()

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

        # Palette: re-extract on track change, then crossfade. Fetch is async —
        # while we wait, the loop keeps rendering with whatever palette we
        # already have so the strip never freezes on a slow Spotify CDN.
        if snap["track_id"] != target_track_id:
            new_palette = palette_worker.get_or_fetch(
                snap["track_id"],
                snap.get("art_url"),
                config["palette_colors"],
                config["saturation_boost"],
            )
            if new_palette:
                target_palette = new_palette
                target_track_id = snap["track_id"]
                crossfade_start = now
                if current_palette is None:
                    current_palette = target_palette

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

        # Spin state mirrors the kiosk vinyl: setSpinTarget(1) on play,
        # setSpinTarget(0) on pause; speed eases over SPIN_RAMP_SECONDS.
        new_target = 1 if is_playing else 0
        if new_target != spin_target:
            spin_target = new_target
            spin_start_time = now
            spin_start_speed = spin_speed
        if spin_speed != spin_target:
            elapsed = now - spin_start_time
            t = min(elapsed / SPIN_RAMP_SECONDS, 1.0)
            eased = _ease_in_out(t)
            spin_speed = spin_start_speed + (spin_target - spin_start_speed) * eased
            if t >= 1.0:
                spin_speed = float(spin_target)

        # Integrate gradient phase by elapsed time × current spin speed.
        # Done every loop iteration (not just on send) so the integration
        # captures the easing accurately rather than sampling at send time.
        drift_period = max(0.5, config["gradient_drift_seconds"])
        dt = max(0.0, now - phase_updated_at)
        rpm_target = RPM_SINGLE_FACTOR if snap.get("is_single") else 1.0
        if rpm_factor != rpm_target:
            rpm_factor += (rpm_target - rpm_factor) * min(1.0, dt / RPM_EASE_SECONDS)
            if abs(rpm_factor - rpm_target) < 0.001:
                rpm_factor = rpm_target
        phase = (phase + spin_speed * rpm_factor * dt / drift_period) % 1.0
        phase_updated_at = now

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
            # Sleep exactly until the next frame is due. No artificial cap —
            # config reload (2 s) and track polling (independent thread, 2 s)
            # don't need sub-50 ms wake-ups, and at low FPS the cap forced
            # extra loop iterations that did nothing but burn CPU.
            time.sleep(max(0.0, interval - (now - last_send)))
            continue

        # Progress fraction for the dim band — only advances while playing.
        duration_ms = max(1, snap["duration_ms"])
        progress_ms = effective_progress_ms(snap)
        progress_frac = max(0.0, min(1.0, progress_ms / duration_ms))

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
