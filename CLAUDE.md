# CLAUDE.md

## Project Overview

Spotify Circular Display — a vinyl-inspired Spotify player for 1080x1080 circular screens on Raspberry Pi. Album artwork fills a spinning vinyl record with grooves, a center label with circular progress ring, synced scrolling lyrics, and playback controls.

## Repository Structure

```
.
├── server.py              # Flask backend — Spotify OAuth2, API proxy, lyrics proxy, serves web UI
├── display.py             # Pygame-based fallback display for low-powered devices (Pi Zero etc.)
├── gpio_buttons.py        # GPIO button handler for physical controls (requires RPi.GPIO, runs as root)
├── templates/
│   └── index.html         # Main web UI — all-in-one HTML/CSS/JS vinyl display (no build step)
├── services/
│   ├── spotify-display.service   # systemd: Flask server
│   ├── spotify-kiosk.service     # systemd: Chromium kiosk mode
│   └── spotify-buttons.service   # systemd: GPIO buttons (optional)
├── setup.sh               # One-shot Pi deployment script (installs deps, configures HDMI, systemd)
├── config.example.json    # Template for Spotify API credentials
├── config.json            # (gitignored) Actual credentials + tokens
├── requirements.txt       # Python deps: Flask, requests, RPi.GPIO
└── demo.gif               # Demo animation for README
```

## Architecture

- **`server.py`** (Flask, port 5000) is the central hub: handles Spotify OAuth2 token flow, proxies all Spotify Web API calls, fetches lyrics from LRCLIB, and serves the web UI.
- **`templates/index.html`** is the primary display — a single self-contained HTML file with inline CSS and JS. No frameworks, no build tools. Uses CSS `transform: rotate()` for GPU-accelerated vinyl spinning, SVG `stroke-dashoffset` for the progress ring, and fetches lyrics via `/api/lyrics`.
- **`display.py`** is a standalone Pygame alternative for devices that can't run Chromium. It polls `server.py` at `localhost:5000` for playback state.
- **`gpio_buttons.py`** listens for physical button presses via RPi.GPIO and sends HTTP requests to `server.py`.
- **Raspotify** (installed by `setup.sh`) provides Spotify Connect audio output so the Pi appears as a speaker.

### Data Flow

```
Spotify App → Spotify Connect (Raspotify) → audio output
Spotify App → Spotify Web API → server.py → /api/* endpoints → index.html (polls every 2s)
                                           → display.py (polls every 2s)
GPIO buttons → gpio_buttons.py → server.py → Spotify Web API
```

## Key API Endpoints (server.py)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/login` | GET | Initiates Spotify OAuth2 flow |
| `/callback` | GET | OAuth2 callback, stores tokens in `config.json` |
| `/api/now-playing` | GET | Current playback state |
| `/api/play` | PUT | Resume playback |
| `/api/pause` | PUT | Pause playback |
| `/api/next` | POST | Skip to next track |
| `/api/previous` | POST | Previous track |
| `/api/shuffle` | PUT | Toggle shuffle (`?state=true/false`) |
| `/api/repeat` | PUT | Set repeat (`?state=off/track/context`) |
| `/api/seek` | PUT | Seek (`?position_ms=N`) |
| `/api/volume` | PUT | Set volume (`?volume_percent=N`) |
| `/api/devices` | GET | List available devices |
| `/api/transfer` | PUT | Transfer playback (JSON body) |
| `/api/lyrics` | GET | Fetch synced lyrics from LRCLIB |

## Development Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask requests
cp config.example.json config.json
# Edit config.json with Spotify client_id and client_secret
python3 server.py
# Visit http://127.0.0.1:5000/login to authenticate
# Then http://127.0.0.1:5000 for the display
```

`RPi.GPIO` is only needed on Raspberry Pi for `gpio_buttons.py`; it is not required for local development of the server or web UI.

## Conventions and Patterns

### Python
- Python 3, no type hints used in this codebase
- No test suite or linter configured
- `config.json` is the sole credential/token store — read/written at runtime by `server.py`
- `config.json` is gitignored; `config.example.json` is the template
- Token refresh is handled automatically in `get_token()`

### Frontend (templates/index.html)
- Single-file architecture: all HTML, CSS, and JS in one file
- Vanilla JS in an IIFE — no modules, no bundler, no framework
- Uses Google Fonts (Montserrat) loaded via CDN
- All constants (sizes, radii, colors) are defined as CSS custom properties and JS constants at the top
- Animation uses `requestAnimationFrame` for the vinyl spin, progress ring, and lyrics
- Polling interval: 2 seconds via `setInterval`

### Display Constants (shared between display.py and index.html)
- Screen size: 1080x1080
- Groove count: 120 lines from radius 100 to 530
- Label radius: 80px (black center)
- Ring radius: 74px (progress arc)
- Spindle radius: 14px
- Pill width: 460px, height: 140px
- Vinyl RPM: 33⅓

### GPIO Pin Mapping (gpio_buttons.py)
- BCM 17: Previous track
- BCM 27: Play/Pause
- BCM 22: Next track
- BCM 23: Volume down
- BCM 24: Volume up
- Debounce: 250ms, Volume step: 5%

## Sensitive Files

- **`config.json`** contains Spotify client_id, client_secret, access_token, refresh_token — never commit this file
- `server.py` uses `os.urandom(24)` for Flask session key (regenerated each restart)

## Deployment

- Target: Raspberry Pi 4 (2GB+) or Pi 5 with 1080x1080 circular HDMI display
- `setup.sh` handles full deployment: system packages, Raspotify, venv, systemd services, HDMI config, initial auth
- Services assume project lives at `/home/pi/spotify-pi/` and runs as user `pi` (except GPIO which runs as root)
- After setup, `sudo reboot` starts everything automatically
