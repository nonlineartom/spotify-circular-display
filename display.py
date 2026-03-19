#!/usr/bin/env python3
"""Vinyl display for round 1080×1080 screen.

Album artwork fills the platter. Progress is shown as a circular arc
around the perimeter. Track info and controls in a compact pill.
"""

import io
import math
import os
import time
import threading
import requests
import pygame
from PIL import Image, ImageDraw

os.environ["SDL_VIDEODRIVER"] = "wayland"
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")

SERVER_URL = "http://localhost:5000"
SCREEN_SIZE = 1080
CENTER = SCREEN_SIZE // 2
FPS = 30
POLL_INTERVAL = 2.0

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
        self.last_update = time.time()

        self.art_surface = None
        self.art_cache_url = ""

        self.lock = threading.Lock()
        self.running = True
        self.last_frame_time = time.time()

        threading.Thread(target=self._poll_loop, daemon=True).start()

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

    def _load_art(self, url):
        try:
            resp = requests.get(url, timeout=10)
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            img = img.resize((ART_SIZE, ART_SIZE), Image.LANCZOS)
            data = img.tobytes()
            surf = pygame.image.fromstring(data, (ART_SIZE, ART_SIZE), "RGB")
            with self.lock:
                self.art_surface = surf
        except Exception as e:
            print(f"Art load error: {e}")

    # ── Spotify polling ─────────────────────────────────────

    def _poll_loop(self):
        while self.running:
            try:
                resp = requests.get(f"{SERVER_URL}/api/now-playing", timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    self._update_state(data)
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)

    def _update_state(self, data):
        if not data or not data.get("item"):
            with self.lock:
                self.track_id = None
            return

        track = data["item"]
        with self.lock:
            self.is_playing = data.get("is_playing", False)
            self.progress_ms = data.get("progress_ms", 0)
            self.duration_ms = track.get("duration_ms", 1)
            self.last_update = time.time()

            new_id = track.get("id")
            if new_id != self.track_id:
                self.track_id = new_id
                self.track_name = track.get("name", "")
                artists = track.get("artists", [])
                self.artist_name = ", ".join(a["name"] for a in artists)

            images = track.get("album", {}).get("images", [])
            art_url = images[0]["url"] if images else ""
            if art_url and art_url != self.art_cache_url:
                self.art_cache_url = art_url
                threading.Thread(
                    target=self._load_art, args=(art_url,), daemon=True
                ).start()

    # ── Touch handling ──────────────────────────────────────

    def _screen_to_canvas(self, pos):
        sx, sy = pos
        ox = (self.display_w - self.render_size) // 2
        oy = (self.display_h - self.render_size) // 2
        return int((sx - ox) / self.scale), int((sy - oy) / self.scale)

    def _handle_touch(self, pos):
        x, y = self._screen_to_canvas(pos)
        px = x - PILL_X
        py = y - PILL_Y

        if 0 <= px <= PILL_WIDTH and 0 <= py <= PILL_HEIGHT:
            pill_cx = PILL_WIDTH // 2
            if CONTROLS_Y - 25 < py < CONTROLS_Y + 25:
                if abs(px - (pill_cx - 80)) < 30:
                    self._api("POST", "/previous")
                    return
                elif abs(px - pill_cx) < 35:
                    with self.lock:
                        playing = self.is_playing
                    self._api("PUT", "/pause" if playing else "/play")
                    return
                elif abs(px - (pill_cx + 80)) < 30:
                    self._api("POST", "/next")
                    return
            with self.lock:
                playing = self.is_playing
            self._api("PUT", "/pause" if playing else "/play")
            return

        with self.lock:
            playing = self.is_playing
        self._api("PUT", "/pause" if playing else "/play")

    def _api(self, method, path):
        try:
            requests.request(method, f"{SERVER_URL}/api{path}", timeout=3)
        except Exception:
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
        # Arc rect — centered, sized for the ring
        inset = CENTER - RING_RADIUS
        arc_rect = pygame.Rect(inset, inset, RING_RADIUS * 2, RING_RADIUS * 2)

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
            now = time.time()
            self.last_frame_time = now

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    self.running = False
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    self._handle_touch(event.pos)

            # ── Draw ──
            self.canvas.fill(BG)

            with self.lock:
                has_track = self.track_id is not None
                art = self.art_surface
                playing = self.is_playing
                progress = self.progress_ms
                if self.is_playing:
                    progress += (time.time() - self.last_update) * 1000
                progress = min(progress, self.duration_ms)
                duration = self.duration_ms

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
            if self.render_size == SCREEN_SIZE:
                self.screen.blit(self.canvas, (ox, oy))
            else:
                self.screen.blit(
                    pygame.transform.scale(self.canvas, (self.render_size, self.render_size)),
                    (ox, oy),
                )

            pygame.display.flip()
            clock.tick(FPS)

        pygame.quit()


if __name__ == "__main__":
    display = SpotifyVinyl()
    display.run()
