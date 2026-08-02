#!/usr/bin/env bash
# Production WSGI launcher shared by systemd and manual diagnostics.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DISPLAY_HOST="${DISPLAY_HOST:-0.0.0.0}"
DISPLAY_PORT="${DISPLAY_PORT:-${PORT:-5000}}"
WAITRESS_THREADS="${WAITRESS_THREADS:-10}"
export PORT="$DISPLAY_PORT"

case "$DISPLAY_PORT" in
    ''|*[!0-9]*) echo "serve: DISPLAY_PORT must be numeric" >&2; exit 2 ;;
esac
case "$WAITRESS_THREADS" in
    ''|*[!0-9]*) echo "serve: WAITRESS_THREADS must be numeric" >&2; exit 2 ;;
esac
if [ "$DISPLAY_PORT" -lt 1 ] || [ "$DISPLAY_PORT" -gt 65535 ]; then
    echo "serve: DISPLAY_PORT must be between 1 and 65535" >&2
    exit 2
fi

cd "$PROJECT_DIR"
exec "$PROJECT_DIR/venv/bin/waitress-serve" \
    --listen="${DISPLAY_HOST}:${DISPLAY_PORT}" \
    --threads="$WAITRESS_THREADS" \
    --channel-timeout=30 \
    --ident="Spotify-Pi-Display" \
    server:app
