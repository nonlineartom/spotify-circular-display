# Spotify Circular Display

A vinyl-inspired Spotify player for circular screens, built for the Raspberry Pi. Album artwork fills a spinning vinyl record with grooves, a center label with a circular progress ring, synced scrolling lyrics, and track info — all rendered in the browser at 60fps.

**Zero-config for guests** — anyone on your network can open Spotify, select "Pi Display", and their music appears on the display. No login or authentication required.

<p align="center">
  <img src="demo.gif" alt="Spotify Circular Display demo" width="400">
</p>

## Features

- **Zero-config playback** — No OAuth login needed. Anyone on the network selects "Pi Display" in Spotify and it just works
- **Local touch controls** — Swipe/tap controls go through the on-device Spotify Connect receiver, not a personal Spotify Web API token
- **Spinning vinyl record** — Album art fills a rotating platter at 33&#8531; RPM with smooth CSS GPU-accelerated animation
- **Eased spin-up/spin-down** — 4-second cubic ease-in-out ramp when playback starts/stops, with return-to-zero when paused
- **Vinyl grooves** — Canvas-rendered concentric groove lines overlaid on the artwork
- **Circular progress ring** — Canvas arc on the center label with an animated dot tip, warm-to-white gradient
- **Synced scrolling lyrics** — Time-synced lyrics from LRCLIB scroll in the top half of the display, with the active line highlighted
- **Track info** — Song title, artist name, and elapsed/remaining time
- **Premium transitions** — Track skips flip the record, metadata crossfades, and the bottom time bar updates smoothly
- **Idle launcher** — When idle, the display offers configurable house playlists that start through the local receiver
- **Spotify Connect** — Acts as a Spotify Connect speaker via go-librespot, with Raspotify kept as a fallback
- **GPIO volume buttons** — Physical buttons for volume up/down via amixer (optional)
- **Auto-start kiosk** — Boots straight into fullscreen Chromium displaying the player
- **1080x1080** — Designed specifically for square/circular displays

## How It Works (For Users)

1. **Open Spotify** on your phone or computer
2. **Tap the devices icon** (bottom of now-playing screen)
3. **Select "Pi Display"**
4. **Play music** — the display shows your artwork, lyrics, and progress instantly

That's it. No accounts to create, no QR codes to scan, no passwords.

## Hardware

| Component | Recommended |
|-----------|------------|
| **Single-board computer** | Raspberry Pi 5 (4GB+) |
| **Display** | 1080x1080 circular HDMI display |
| **Audio** | Built-in audio jack, USB DAC, or HDMI audio |
| **Buttons** (optional) | Momentary push buttons wired to GPIO |

> **Note:** A Pi 4 (2GB+) or Pi 5 is recommended for smooth Chromium rendering. A pygame-based fallback (`display.py`) is included for lower-powered devices.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Any Spotify App (phone/computer)               │
│  Select "Pi Display" as output device           │
└──────────────┬──────────────────────────────────┘
               │ Spotify Connect
┌──────────────▼──────────────────────────────────┐
│  Raspberry Pi                                   │
│                                                 │
│  ┌─────────────┐                                │
│  │go-librespot │── local API ─► playback state  │
│  │ (audio out) │   (track, position, controls)  │
│  └─────────────┘                                │
│                   ┌──────────────────────────┐  │
│                   │ Flask Server (server.py)  │  │
│  localhost:3678 ► │ - Reads local state       │  │
│                   │ - Track metadata lookup   │  │
│                   │   (client credentials)    │  │
│                   │ - Lyrics proxy (LRCLIB)   │  │
│                   │ - Serves web UI           │  │
│                   └──────────┬───────────────┘  │
│                              │ localhost:5000    │
│  ┌───────────────────────────▼───────────────┐  │
│  │ Chromium Kiosk (fullscreen)               │  │
│  │ - HTML/CSS/JS vinyl display               │  │
│  │ - 60fps CSS rotation                      │  │
│  │ - Canvas progress ring                    │  │
│  │ - Synced lyrics                           │  │
│  └───────────────────────────────────────────┘  │
│                                                 │
│  ┌───────────────────────────────────────────┐  │
│  │ GPIO Buttons (optional)                   │  │
│  │ BCM 23=vol-, 24=vol+ (via amixer)         │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

### How metadata works without user login

1. **go-librespot** receives audio via Spotify Connect and exposes local playback state/control endpoints on `127.0.0.1:3678`
2. **Flask server** reads the local receiver API first, falling back to the old Raspotify `/tmp/spotify-state.json` event file on older installs
3. **Touch controls** call the local receiver through Flask, so skip/pause does not need per-user Spotify Web API OAuth
4. **Frontend** polls `/api/now-playing` every 2 seconds and renders the vinyl display

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/nonlineartom/spotify-circular-display.git
cd spotify-circular-display
```

### 2. Create a Spotify App

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Create a new app
3. Note your **Client ID** and **Client Secret**

> No redirect URI is needed for the main display controls. OAuth is only needed
> if you enable the optional phone sign-in / personalized playlist flow.

### 3. Configure

```bash
cp config.example.json config.json
```

Edit `config.json` with your Spotify app credentials:

```json
{
  "client_id": "YOUR_SPOTIFY_CLIENT_ID",
  "client_secret": "YOUR_SPOTIFY_CLIENT_SECRET",
  "public_base_url": "",
  "redirect_uri": ""
}
```

For optional Spotify sign-in, Spotify requires the redirect URI sent by the app
to exactly match a URI in the Spotify Developer Dashboard. Set one of these:

- `redirect_uri`: exact full callback URI, for example `https://your-domain.example/callback`
- `public_base_url`: base URL for the display, used to build `/callback` and `/join`

Spotify currently requires HTTPS for non-loopback redirects. A plain LAN URL
like `http://192.168.68.80:5000/callback` may be rejected for newly created
Spotify apps. Use an HTTPS tunnel/domain for phone sign-in, or leave OAuth off
and keep using the zero-config local Spotify Connect controls.

### 4. Deploy to Raspberry Pi

Copy the project to your Pi and run the setup script:

```bash
scp -r . admin@your-pi-ip:~/circle-pi-display/
ssh admin@your-pi-ip
cd ~/circle-pi-display
chmod +x setup.sh
./setup.sh
```

The setup script will:
- Install system dependencies (Python, Chromium, unclutter)
- Install and configure go-librespot as the primary Spotify Connect receiver
- Install Raspotify as a disabled fallback receiver for older setups
- Create a Python virtual environment and install packages
- Prompt for Spotify API credentials (if not already in config.json)
- Install systemd services for auto-start
- Configure HDMI output for 1080x1080

### 5. Reboot and enjoy

```bash
sudo reboot
```

After reboot, open Spotify on your phone, select **"Pi Display"** as the output device, and play music. The display updates instantly.

## Idle Launcher

Copy `idle_playlists.example.json` to `idle_playlists.json` and edit the Spotify playlist URIs, titles, subtitles, and accent colors. When the display is idle, those cards appear on the circular interface. Tapping a card starts the playlist through go-librespot's local `/player/play` endpoint. If someone signs in from `/join`, the launcher prefers that account's playlists and fills any spare cards with the house defaults.

## Systemd Services

| Service | Description |
|---------|------------|
| `spotify-display` | Flask server — metadata lookup and web UI |
| `spotify-kiosk` | Chromium in fullscreen kiosk mode |
| `spotify-buttons` | GPIO button handler (optional) |
| `spotify-network-watchdog` | Restarts Spotify services after Wi-Fi returns |
| `go-librespot` | Spotify Connect audio receiver + local state/control API |
| `raspotify` | Disabled fallback Spotify Connect receiver + onevent |

Useful commands:

```bash
sudo systemctl status spotify-display
sudo systemctl status go-librespot
sudo systemctl restart spotify-kiosk
sudo journalctl -u spotify-display -f
sudo journalctl -u go-librespot -f
curl http://localhost:5000/api/health
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
| 23 | Volume down (amixer) |
| 24 | Volume up (amixer) |

> Play/pause, next, and previous controls can be routed through the local go-librespot API. Volume buttons still use `amixer`.

Internal pull-up resistors are enabled — no external resistors needed.

## Tech Stack

- **Backend:** Python / Flask — local receiver API proxy, fallback state reader, Spotify client credentials for metadata, LRCLIB lyrics proxy
- **Metadata/Controls:** go-librespot local API, with Raspotify `--onevent` fallback support
- **Frontend:** Vanilla HTML/CSS/JS — no build tools or frameworks
- **Animation:** CSS `transform: rotate()` with `will-change` for GPU compositing
- **Progress:** Canvas-based circular arc with warm-to-white gradient
- **Lyrics:** [LRCLIB](https://lrclib.net) — free time-synced lyrics API
- **Audio:** [go-librespot](https://github.com/devgianlu/go-librespot) (librespot-based Spotify Connect)
- **Fonts:** [Montserrat](https://fonts.google.com/specimen/Montserrat) via Google Fonts

## License

MIT
