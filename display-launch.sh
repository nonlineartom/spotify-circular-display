#!/usr/bin/env bash
# Graphical-session launcher for Chromium (default) or the pygame fallback.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-chromium}"
DISPLAY_PORT="${DISPLAY_PORT:-${PORT:-5000}}"
SPOTIFY_DISPLAY_URL="${SPOTIFY_DISPLAY_URL:-http://127.0.0.1:${DISPLAY_PORT}}"
export SPOTIFY_DISPLAY_URL

for _attempt in $(seq 1 90); do
    if curl --silent --fail --max-time 1 \
            "${SPOTIFY_DISPLAY_URL}/api/info" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
if ! curl --silent --fail --max-time 2 \
        "${SPOTIFY_DISPLAY_URL}/api/info" >/dev/null 2>&1; then
    echo "display-launch: web server did not become ready" >&2
    exit 1
fi

XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    for socket_path in "$XDG_RUNTIME_DIR"/wayland-*; do
        if [ -S "$socket_path" ]; then
            WAYLAND_DISPLAY="$(basename "$socket_path")"
            export WAYLAND_DISPLAY
            break
        fi
    done
fi

if [ "$MODE" = "pygame" ]; then
    if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
        echo "display-launch: no active Wayland or X11 graphical session" >&2
        exit 1
    fi
    exec "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/display.py"
fi
if [ "$MODE" != "chromium" ]; then
    echo "display-launch: expected chromium or pygame, got '$MODE'" >&2
    exit 2
fi

CHROMIUM=""
for candidate in chromium chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then
        CHROMIUM="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$CHROMIUM" ]; then
    echo "display-launch: Chromium is not installed" >&2
    exit 1
fi

OZONE_ARGS=()
if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]; then
    OZONE_ARGS+=(--ozone-platform=wayland)
elif [ -z "${DISPLAY:-}" ]; then
    echo "display-launch: no active Wayland or X11 graphical session" >&2
    exit 1
fi

exec "$CHROMIUM" \
    --no-first-run \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-restore-session-state \
    --disable-pinch \
    --kiosk \
    --incognito \
    --disable-translate \
    --disable-features=TranslateUI \
    --password-store=basic \
    --overscroll-history-navigation=0 \
    --disk-cache-size=48000000 \
    --window-size=1080,1080 \
    --window-position=0,0 \
    "${OZONE_ARGS[@]}" \
    "$SPOTIFY_DISPLAY_URL"
