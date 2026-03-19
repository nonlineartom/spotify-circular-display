# Spotify Circular Display

A vinyl-inspired Spotify player for circular screens, built for the Raspberry Pi. Album artwork fills a spinning vinyl record with grooves, a center label with a circular progress ring, synced scrolling lyrics, and playback controls — all rendered in the browser at 60fps.

<p align="center">
  <img src="demo.gif" alt="Spotify Circular Display demo" width="400">
</p>

## Features

- **Spinning vinyl record** — Album art fills a rotating platter at 33⅓ RPM with smooth CSS GPU-accelerated animation
- **Eased spin-up/spin-down** — 4-second cubic ease-in-out ramp when playback starts/stops, with return-to-zero when paused
- **Vinyl grooves** — Canvas-rendered concentric groove lines overlaid on the artwork
- **Circular progress ring** — SVG arc on the center label with an animated dot tip, showing track progress
- **Synced scrolling lyrics** — Time-synced lyrics from LRCLIB scroll in the top half of the display, with the active line highlighted
- **Track info** — Song title, artist name, and elapsed/remaining time with text shadows for readability
- **Playback controls** — Previous, play/pause, and next buttons; tap anywhere to toggle playback
- **Spotify Connect** — Acts as a Spotify Connect speaker via Raspotify, so you can cast from any Spotify app
- **GPIO button support** — Physical buttons for previous, play/pause, next, volume up/down (optional)
- **Auto-start kiosk** — Boots straight into fullscreen Chromium displaying the player
- **Responsive to 1080x1080** — Designed specifically for square/circular displays

## Hardware

| Component | Recommended |
|-----------|------------|
| **Single-board computer** | Raspberry Pi 5 (4GB+) |
| **Display** | 1080x1080 circular HDMI display |
| **Audio** | Built-in audio jack, USB DAC, or HDMI audio |
| **Buttons** (optional) | Momentary push buttons wired to GPIO |

> **Note:** The Raspberry Pi Zero 2 W does not have enough RAM or GPU capability to run Chromium smoothly. A Pi 4 (2GB+) or Pi 5 is recommended. A pygame-based fallback display (`display.py`) is included for lower-powered devices.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Spotify App (phone/computer)                   │
│  Select "Pi Display" as output device           │
└──────────────┬──────────────────────────────────┘
               │ Spotify Connect
┌──────────────▼──────────────────────────────────┐
│  Raspberry Pi                                   │
│                                                 │
│  ┌─────────────┐  ┌──────────────────────────┐  │
│  │ Raspotify   │  │ Flask Server (server.py)  │  │
│  │ (audio out) │  │ - Spotify OAuth2          │  │
│  └─────────────┘  │ - API proxy               │  │
│                   │ - Lyrics proxy (LRCLIB)   │  │
│                   │ - Serves web UI           │  │
│                   └──────────┬───────────────┘  │
│                              │ localhost:5000    │
│  ┌───────────────────────────▼───────────────┐  │
│  │ Chromium Kiosk (fullscreen)               │  │
│  │ - HTML/CSS/JS vinyl display               │  │
│  │ - 60fps CSS rotation                      │  │
│  │ - SVG progress ring                       │  │
│  │ - Synced lyrics                           │  │
│  └───────────────────────────────────────────┘  │
│                                                 │
│  ┌───────────────────────────────────────────┐  │
│  │ GPIO Buttons (optional)                   │  │
│  │ BCM 17=prev, 27=play, 22=next            │  │
│  │ BCM 23=vol-, 24=vol+                     │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/spotify-circular-display.git
cd spotify-circular-display
```

### 2. Create a Spotify App

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Create a new app
3. Add `http://127.0.0.1:5000/callback` as a **Redirect URI**
4. Note your **Client ID** and **Client Secret**

### 3. Configure

```bash
cp config.example.json config.json
```

Edit `config.json` with your Spotify credentials:

```json
{
  "client_id": "YOUR_SPOTIFY_CLIENT_ID",
  "client_secret": "YOUR_SPOTIFY_CLIENT_SECRET",
  "redirect_uri": "http://127.0.0.1:5000/callback",
  "access_token": "",
  "refresh_token": "",
  "token_expiry": 0
}
```

### 4. Deploy to Raspberry Pi

Copy the project to your Pi and run the setup script:

```bash
scp -r . pi@raspberrypi.local:~/spotify-pi/
ssh pi@raspberrypi.local
cd ~/spotify-pi
chmod +x setup.sh
./setup.sh
```

The setup script will:
- Install system dependencies (Python, Chromium, unclutter)
- Install and configure Raspotify as a Spotify Connect receiver
- Create a Python virtual environment and install packages
- Prompt for Spotify API credentials (if not already in config.json)
- Install systemd services for auto-start
- Configure HDMI output for 1080x1080
- Walk you through initial Spotify authentication

### 5. Reboot and enjoy

```bash
sudo reboot
```

After reboot, open Spotify on your phone or computer, select **"Pi Display"** as the output device, and play some music.

## Running Locally (for development)

You can test the display on any computer with Python 3 and a browser:

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask requests
cp config.example.json config.json
# Edit config.json with your Spotify credentials
python3 server.py
```

Open `http://127.0.0.1:5000/login` to authenticate, then visit `http://127.0.0.1:5000` to see the display.

## Systemd Services

| Service | Description |
|---------|------------|
| `spotify-display` | Flask server — API proxy and web UI |
| `spotify-kiosk` | Chromium in fullscreen kiosk mode |
| `spotify-buttons` | GPIO button handler (optional) |
| `raspotify` | Spotify Connect audio receiver |

Useful commands:

```bash
sudo systemctl status spotify-display
sudo systemctl restart spotify-kiosk
sudo journalctl -u spotify-display -f
```

## Display Configuration

The setup script configures HDMI for a 1080x1080 square display. If your display has different specs, edit `/boot/firmware/config.txt`:

```ini
hdmi_force_hotplug=1
hdmi_group=2
hdmi_mode=87
hdmi_cvt=1080 1080 60 1 0 0 0
```

## GPIO Pinout (optional)

Wire momentary buttons between these BCM GPIO pins and GND:

| Pin | Function |
|-----|----------|
| 17 | Previous track |
| 27 | Play / Pause |
| 22 | Next track |
| 23 | Volume down |
| 24 | Volume up |

Internal pull-up resistors are enabled — no external resistors needed.

## Tech Stack

- **Backend:** Python / Flask — Spotify OAuth2 flow, API proxy, lyrics proxy
- **Frontend:** Vanilla HTML/CSS/JS — no build tools or frameworks
- **Animation:** CSS `transform: rotate()` with `will-change` for GPU compositing
- **Progress:** SVG `stroke-dashoffset` circular arc
- **Lyrics:** [LRCLIB](https://lrclib.net) — free time-synced lyrics API
- **Audio:** [Raspotify](https://github.com/dtcooper/raspotify) (librespot-based Spotify Connect)
- **Fonts:** [Montserrat](https://fonts.google.com/specimen/Montserrat) via Google Fonts

## License

MIT
