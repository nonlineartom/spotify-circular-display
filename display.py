#!/usr/bin/env python3
"""Vinyl display for round 1080×1080 screen.

Album artwork fills the platter. Progress is shown as a circular arc
around the perimeter. Track info and controls in a compact pill.
"""

import io
import math
import os
import queue
import time
import threading
import requests

# Respect the graphical session chosen by systemd/SDL. A driver can still be
# forced for diagnostics, but Wayland and UID 1000 are no longer hard-coded.
if os.environ.get("SPOTIFY_DISPLAY_SDL_DRIVER"):
    os.environ["SDL_VIDEODRIVER"] = os.environ["SPOTIFY_DISPLAY_SDL_DRIVER"]

import pygame
from PIL import Image

SERVER_URL = os.environ.get("SPOTIFY_DISPLAY_URL", "http://127.0.0.1:5000").rstrip("/")
SCREEN_SIZE = 1080
CENTER = SCREEN_SIZE // 2
FPS = max(1, min(60, int(os.environ.get("SPOTIFY_DISPLAY_FPS", "30"))))
PAUSED_FPS = max(1, min(FPS, int(os.environ.get("SPOTIFY_DISPLAY_PAUSED_FPS", "5"))))
IDLE_FPS = max(1, min(PAUSED_FPS, int(os.environ.get("SPOTIFY_DISPLAY_IDLE_FPS", "1"))))
POLL_INTERVAL = max(0.5, float(os.environ.get("SPOTIFY_DISPLAY_POLL_SECONDS", "2")))
DIM_AFTER_SECONDS = max(0, float(os.environ.get("SPOTIFY_DISPLAY_DIM_SECONDS", "300")))
ART_RETRY_SECONDS = 30.0

ART_SIZE = SCREEN_SIZE

# Vinyl overlay
GROOVE_COUNT = 120
GROOVE_START = 100
GROOVE_END = 530
SPINDLE_RADIUS = 14
LABEL_RADIUS = 80

# Circular progress ring
RING_RADIUS = LABEL_RADIUS - 6
RING_WIDTH = 4
RING_BG_ALPHA = 40

# Player pill dimensions (compact — no progress bar)
PILL_WIDTH = 460
PILL_HEIGHT = 140
PILL_RADIUS = 22
PILL_ALPHA = 180
PILL_X = (SCREEN_SIZE - PILL_WIDTH) // 2
PILL_Y = SCREEN_SIZE - PILL_HEIGHT - 110

# Layout within the pill (relative to pill top)
TRACK_Y = 18
ARTIST_Y = 50
TIME_Y = 78
CONTROLS_Y = 112

# Colors
BG = (0, 0, 0)
WHITE = (255, 255, 255)
DIM = (180, 180, 180)
SPINDLE_COLOR = (50, 50, 50)


class SpotifyVinyl:
    def __init__(self):
        pygame.init()
        pygame.mouse.set_visible(False)

        info = pygame.display.Info()
        self.display_w = info.current_w
        self.display_h = info.current_h
        self.render_size = min(self.display_w, self.display_h, SCREEN_SIZE)
        self.scale = self.render_size / SCREEN_SIZE

        self.screen = pygame.display.set_mode(
            (self.display_w, self.display_h), pygame.FULLSCREEN | pygame.NOFRAME
        )
        self.canvas = pygame.Surface((SCREEN_SIZE, SCREEN_SIZE))

        # Fonts — Montserrat
        try:
            self.font_track = pygame.font.Font("/usr/share/fonts/truetype/montserrat/Montserrat-Medium.ttf", 28)
            self.font_artist = pygame.font.Font("/usr/share/fonts/truetype/montserrat/Montserrat-Light.ttf", 19)
            self.font_time = pygame.font.Font("/usr/share/fonts/truetype/montserrat/Montserrat-Light.ttf", 14)
            self.font_idle = pygame.font.Font("/usr/share/fonts/truetype/montserrat/Montserrat-Light.ttf", 26)
        except Exception:
            self.font_track = pygame.font.SysFont("sans", 30, bold=True)
            self.font_artist = pygame.font.SysFont("sans", 20)
            self.font_time = pygame.font.SysFont("sans", 16)
            self.font_idle = pygame.font.SysFont("sans", 28)

        # Build static overlays
        self.groove_overlay = self._build_groove_overlay()
        self.circle_mask = self._build_circle_mask()
        self.pill_bg = self._build_pill()
        self._build_control_icons()

        # Playback state
        self.is_playing = False
        self.track_id = None
        self.track_name = ""
        self.artist_name = ""
        self.progress_ms = 0
        self.duration_ms = 1
        self.last_update = time.monotonic()

        self.art_surface = None
        self.art_cache_url = ""
        self.art_requested_url = ""
        self.art_generation = 0
        self.art_failed_at = 0.0
        self._art_result = None
        self._art_error_log = {}
        self._art_queue = queue.Queue(maxsize=1)
        self._control_queue = queue.Queue(maxsize=8)

        self.lock = threading.Lock()
        self.running = True
        self.last_frame_time = time.monotonic()
        self.last_activity = time.monotonic()
        self.dimmed = False
        self.last_finger_at = 0.0
        self.last_poll_error_log = 0.0

        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="display-poll"
        )
        self._art_thread = threading.Thread(
            target=self._art_loop, daemon=True, name="display-art"
        )
        self._control_thread = threading.Thread(
            target=self._control_loop, daemon=True, name="display-controls"
        )
        self._poll_thread.start()
        self._art_thread.start()
        self._control_thread.start()

    # ── Static overlays ─────────────────────────────────────

    def _build_groove_overlay(self):
        surf = pygame.Surface((SCREEN_SIZE, SCREEN_SIZE), pygame.SRCALPHA)
        spacing = (GROOVE_END - GROOVE_START) / GROOVE_COUNT
        for i in range(GROOVE_COUNT):
            r = int(GROOVE_START + i * spacing)
            alpha = 15 + (i % 3) * 5
            pygame.draw.circle(surf, (0, 0, 0, alpha), (CENTER, CENTER), r, 1)
        pygame.draw.circle(surf, (0, 0, 0, 40), (CENTER, CENTER), SCREEN_SIZE // 2 - 2, 2)
        # Black vinyl label center
        pygame.draw.circle(surf, (0, 0, 0, 255), (CENTER, CENTER), LABEL_RADIUS)
        return surf

    def _build_circle_mask(self):
        surf = pygame.Surface((SCREEN_SIZE, SCREEN_SIZE), pygame.SRCALPHA)
        surf.fill((0, 0, 0, 255))
        pygame.draw.circle(surf, (0, 0, 0, 0), (CENTER, CENTER), CENTER)
        return surf

    def _build_pill(self):
        surf = pygame.Surface((PILL_WIDTH, PILL_HEIGHT), pygame.SRCALPHA)
        rect = pygame.Rect(0, 0, PILL_WIDTH, PILL_HEIGHT)
        pygame.draw.rect(surf, (0, 0, 0, PILL_ALPHA), rect, border_radius=PILL_RADIUS)
        return surf

    def _build_control_icons(self):
        self.icons = {}
        s = pygame.Surface((32, 32), pygame.SRCALPHA)
        pygame.draw.rect(s, WHITE, (2, 4, 4, 24))
        pygame.draw.polygon(s, WHITE, [(28, 4), (28, 28), (8, 16)])
        self.icons["prev"] = s
        s = pygame.Surface((40, 40), pygame.SRCALPHA)
        pygame.draw.polygon(s, WHITE, [(8, 4), (8, 36), (36, 20)])
        self.icons["play"] = s
        s = pygame.Surface((40, 40), pygame.SRCALPHA)
        pygame.draw.rect(s, WHITE, (8, 4, 8, 32), border_radius=2)
        pygame.draw.rect(s, WHITE, (24, 4, 8, 32), border_radius=2)
        self.icons["pause"] = s
        s = pygame.Surface((32, 32), pygame.SRCALPHA)
        pygame.draw.polygon(s, WHITE, [(4, 4), (4, 28), (24, 16)])
        pygame.draw.rect(s, WHITE, (26, 4, 4, 24))
        self.icons["next"] = s

    # ── Artwork loading ─────────────────────────────────────

    def _queue_art(self, url):
        now = time.monotonic()
        with self.lock:
            if not url or url == self.art_cache_url:
                return
            if url == self.art_requested_url:
                if self.art_failed_at == 0.0 or now - self.art_failed_at < ART_RETRY_SECONDS:
                    return
            self.art_generation += 1
            generation = self.art_generation
            self.art_requested_url = url
            self.art_failed_at = 0.0
        job = (generation, url)
        try:
            self._art_queue.put_nowait(job)
        except queue.Full:
            try:
                self._art_queue.get_nowait()
            except queue.Empty:
                pass
            self._art_queue.put_nowait(job)

    def _art_loop(self):
        """Download only the newest queued artwork; pygame conversion stays main-thread."""
        while self.running:
            try:
                job = self._art_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            generation, url = job
            try:
                resp = requests.get(url, timeout=8)
                resp.raise_for_status()
                img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                img = img.resize((ART_SIZE, ART_SIZE), Image.Resampling.LANCZOS)
                result = (generation, url, img.tobytes())
                with self.lock:
                    if generation == self.art_generation and url == self.art_requested_url:
                        self._art_result = result
            except (requests.RequestException, OSError, ValueError) as exc:
                now = time.monotonic()
                with self.lock:
                    if generation == self.art_generation:
                        self.art_failed_at = now
                    last_log = self._art_error_log.get(url)
                    should_log = last_log is None or now - last_log >= 60.0
                    if should_log:
                        self._art_error_log[url] = now
                        if len(self._art_error_log) > 16:
                            oldest = min(self._art_error_log, key=self._art_error_log.get)
                            self._art_error_log.pop(oldest, None)
                if should_log:
                    print(f"display: artwork load failed: {exc}", flush=True)

    def _consume_art_result(self):
        with self.lock:
            result = self._art_result
            self._art_result = None
        if result is None:
            return
        generation, url, data = result
        surface = pygame.image.fromstring(data, (ART_SIZE, ART_SIZE), "RGB")
        with self.lock:
            if generation == self.art_generation and url == self.art_requested_url:
                self.art_surface = surface
                self.art_cache_url = url
                self.art_failed_at = 0.0

    # ── Spotify polling ─────────────────────────────────────

    def _poll_loop(self):
        while self.running:
            try:
                resp = requests.get(f"{SERVER_URL}/api/now-playing", timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if not isinstance(data, dict) or not isinstance(data.get("item"), dict):
                        raise ValueError("now-playing response is not an active playback object")
                    self._update_state(data)
                elif resp.status_code == 204:
                    self._update_state(None)
                else:
                    self._log_poll_error(f"HTTP {resp.status_code}")
            except (requests.RequestException, ValueError) as exc:
                # Retain the last-known screen during a transient network error.
                self._log_poll_error(str(exc))
            time.sleep(POLL_INTERVAL)

    def _log_poll_error(self, detail):
        now = time.monotonic()
        if not self.last_poll_error_log or now - self.last_poll_error_log >= 30.0:
            print(f"display: playback poll failed: {detail}", flush=True)
            self.last_poll_error_log = now

    def _update_state(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("item"), dict):
            with self.lock:
                self.track_id = None
                self.is_playing = False
                self.track_name = ""
                self.artist_name = ""
                self.progress_ms = 0
                self.duration_ms = 1
            return

        track = data["item"]
        album = track.get("album") if isinstance(track.get("album"), dict) else {}
        images = album.get("images") if isinstance(album.get("images"), list) else []
        art_url = ""
        if images and isinstance(images[0], dict) and isinstance(images[0].get("url"), str):
            art_url = images[0]["url"]
        try:
            progress_ms = max(0, int(data.get("progress_ms") or 0))
        except (TypeError, ValueError, OverflowError):
            progress_ms = 0
        try:
            duration_ms = max(1, int(track.get("duration_ms") or 1))
        except (TypeError, ValueError, OverflowError):
            duration_ms = 1
        artists = track.get("artists") if isinstance(track.get("artists"), list) else []
        artist_names = [
            artist.get("name", "")
            for artist in artists
            if isinstance(artist, dict) and isinstance(artist.get("name"), str)
        ]
        new_id = track.get("id") if isinstance(track.get("id"), str) else None
        with self.lock:
            was_playing = self.is_playing
            self.is_playing = data.get("is_playing") is True
            if was_playing and not self.is_playing:
                self.last_activity = time.monotonic()
            self.progress_ms = progress_ms
            self.duration_ms = duration_ms
            self.last_update = time.monotonic()

            if new_id != self.track_id:
                self.track_id = new_id
                self.track_name = track.get("name") if isinstance(track.get("name"), str) else ""
                self.artist_name = ", ".join(filter(None, artist_names))
                if art_url != self.art_cache_url:
                    self.art_surface = None
        self._queue_art(art_url)

    # ── Touch handling ──────────────────────────────────────

    def _screen_to_canvas(self, pos):
        sx, sy = pos
        ox = (self.display_w - self.render_size) // 2
        oy = (self.display_h - self.render_size) // 2
        if not (ox <= sx < ox + self.render_size and oy <= sy < oy + self.render_size):
            return None
        point = int((sx - ox) / self.scale), int((sy - oy) / self.scale)
        if math.hypot(point[0] - CENTER, point[1] - CENTER) > CENTER:
            return None
        return point

    def _handle_touch(self, pos):
        self.last_activity = time.monotonic()
        if self.dimmed:
            self.dimmed = False
            return
        point = self._screen_to_canvas(pos)
        if point is None:
            return
        x, y = point
        with self.lock:
            has_track = self.track_id is not None
        if not has_track:
            return
        px = x - PILL_X
        py = y - PILL_Y

        if 0 <= px <= PILL_WIDTH and 0 <= py <= PILL_HEIGHT:
            pill_cx = PILL_WIDTH // 2
            if CONTROLS_Y - 25 < py < CONTROLS_Y + 25:
                if abs(px - (pill_cx - 80)) < 30:
                    self._queue_api("POST", "/control/previous")
                    return
                elif abs(px - pill_cx) < 35:
                    self._queue_api("POST", "/control/play-pause")
                    return
                elif abs(px - (pill_cx + 80)) < 30:
                    self._queue_api("POST", "/control/next")
                    return
            self._queue_api("POST", "/control/play-pause")
            return

        # A full-disc tap is too easy to trigger while cleaning/adjusting the
        # display. Only the explicit pill and center label are controls.
        if math.hypot(x - CENTER, y - CENTER) <= LABEL_RADIUS + 20:
            self._queue_api("POST", "/control/play-pause")

    def _queue_api(self, method, path, payload=None):
        try:
            self._control_queue.put_nowait((method, path, payload))
        except queue.Full:
            print("display: control queue full; dropping tap", flush=True)

    def _control_loop(self):
        """Keep network control latency off the pygame render/event thread."""
        while self.running:
            try:
                job = self._control_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            method, path, payload = job
            try:
                response = requests.request(
                    method,
                    f"{SERVER_URL}/api{path}",
                    json=payload,
                    timeout=3,
                )
                if response.status_code >= 400:
                    print(
                        f"display: control {path} failed HTTP {response.status_code}",
                        flush=True,
                    )
            except requests.RequestException as exc:
                print(f"display: control {path} failed: {exc}", flush=True)

    def close(self):
        self.running = False
        try:
            self._art_queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self._control_queue.put_nowait(None)
        except queue.Full:
            pass

    # ── Drawing helpers ─────────────────────────────────────

    def _draw_centered_text(self, surface, text, font, color, x_center, y, max_width):
        rendered = font.render(text, True, color)
        if rendered.get_width() > max_width:
            while rendered.get_width() > max_width and len(text) > 3:
                text = text[:-4] + "..."
                rendered = font.render(text, True, color)
        surface.blit(rendered, (x_center - rendered.get_width() // 2, y))

    def _draw_progress_ring(self, pct):
        """Draw a circular progress arc around the perimeter.

        Starts at 12 o'clock (top), sweeps clockwise.
        pygame.draw.arc uses radians, counterclockwise from 3 o'clock.
        So we convert: start at pi/2 (12 o'clock), sweep clockwise.
        """
        inset = CENTER - RING_RADIUS

        # Background track (subtle ring around the label)
        pygame.draw.circle(self.canvas, (60, 60, 60),
                           (CENTER, CENTER), RING_RADIUS, 1)

        if pct <= 0:
            return

        # Arc from 12 o'clock, clockwise
        start_angle = math.pi / 2
        sweep = pct * 2 * math.pi
        end_angle = start_angle - sweep

        # Draw the progress arc
        for w in range(RING_WIDTH):
            r = pygame.Rect(inset - w, inset - w,
                            RING_RADIUS * 2 + w * 2, RING_RADIUS * 2 + w * 2)
            pygame.draw.arc(self.canvas, WHITE, r, end_angle, start_angle, 1)

        # Ball at the tip of the progress arc
        tip_angle = start_angle - sweep  # current position in radians
        ball_x = int(CENTER + RING_RADIUS * math.cos(tip_angle))
        ball_y = int(CENTER - RING_RADIUS * math.sin(tip_angle))
        pygame.draw.circle(self.canvas, WHITE, (ball_x, ball_y), 6)

    def _draw_player_pill(self, progress, duration, playing):
        """Draw the compact player pill with track info, time, and controls."""
        pill = self.pill_bg.copy()
        pill_cx = PILL_WIDTH // 2
        text_max = PILL_WIDTH - 40

        with self.lock:
            track = self.track_name
            artist = self.artist_name

        # Track name
        self._draw_centered_text(pill, track, self.font_track, WHITE,
                                  pill_cx, TRACK_Y, text_max)
        # Artist
        self._draw_centered_text(pill, artist, self.font_artist, DIM,
                                  pill_cx, ARTIST_Y, text_max)

        # Time (elapsed / remaining centered)
        fmt = lambda ms: f"{max(0,int(ms/1000))//60}:{max(0,int(ms/1000))%60:02d}"
        time_str = f"{fmt(progress)}  /  -{fmt(duration - progress)}"
        self._draw_centered_text(pill, time_str, self.font_time, DIM,
                                  pill_cx, TIME_Y, text_max)

        # Control icons
        for name, x_off in [("prev", -80), ("pause" if playing else "play", 0), ("next", 80)]:
            icon = self.icons[name]
            ix = pill_cx + x_off - icon.get_width() // 2
            iy = CONTROLS_Y - icon.get_height() // 2
            pill.blit(icon, (ix, iy))

        self.canvas.blit(pill, (PILL_X, PILL_Y))

    # ── Main loop ───────────────────────────────────────────

    def run(self):
        clock = pygame.time.Clock()

        while self.running:
            now = time.monotonic()
            self.last_frame_time = now

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    self.running = False
                elif event.type == pygame.FINGERDOWN:
                    self.last_finger_at = now
                    self._handle_touch(
                        (event.x * self.display_w, event.y * self.display_h)
                    )
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    # SDL may synthesize a mouse event after FINGERDOWN.
                    if now - self.last_finger_at > 0.25:
                        self._handle_touch(event.pos)

            self._consume_art_result()

            # ── Draw ──
            self.canvas.fill(BG)

            with self.lock:
                has_track = self.track_id is not None
                art = self.art_surface
                playing = self.is_playing
                progress = self.progress_ms
                if self.is_playing:
                    progress += (time.monotonic() - self.last_update) * 1000
                progress = min(progress, self.duration_ms)
                duration = self.duration_ms

            self.dimmed = bool(
                DIM_AFTER_SECONDS
                and not playing
                and now - self.last_activity >= DIM_AFTER_SECONDS
            )

            if has_track and art:
                # Artwork
                self.canvas.blit(art, (0, 0))

                # Groove overlay
                self.canvas.blit(self.groove_overlay, (0, 0))

                # Circle mask
                self.canvas.blit(self.circle_mask, (0, 0))

                # Spindle
                pygame.draw.circle(self.canvas, SPINDLE_COLOR, (CENTER, CENTER), SPINDLE_RADIUS)
                pygame.draw.circle(self.canvas, (30, 30, 30), (CENTER, CENTER), SPINDLE_RADIUS + 1, 1)
                pygame.draw.circle(self.canvas, (70, 70, 70), (CENTER, CENTER), SPINDLE_RADIUS - 4, 1)

                # Player pill
                self._draw_player_pill(progress, duration, playing)

                # Circular progress ring (drawn last, on top of everything)
                pct = progress / max(duration, 1)
                self._draw_progress_ring(pct)

            else:
                msg = "Waiting for playback..." if not has_track else "Loading..."
                rendered = self.font_idle.render(msg, True, DIM)
                self.canvas.blit(rendered, (CENTER - rendered.get_width() // 2, CENTER - 14))

            # Output to display
            self.screen.fill(BG)
            ox = (self.display_w - self.render_size) // 2
            oy = (self.display_h - self.render_size) // 2
            if not self.dimmed:
                if self.render_size == SCREEN_SIZE:
                    self.screen.blit(self.canvas, (ox, oy))
                else:
                    self.screen.blit(
                        pygame.transform.smoothscale(
                            self.canvas, (self.render_size, self.render_size)
                        ),
                        (ox, oy),
                    )

            pygame.display.flip()
            target_fps = FPS if playing else (PAUSED_FPS if has_track else IDLE_FPS)
            if self.dimmed:
                target_fps = IDLE_FPS
            clock.tick(target_fps)

        self.close()
        pygame.quit()


if __name__ == "__main__":
    display = SpotifyVinyl()
    display.run()
