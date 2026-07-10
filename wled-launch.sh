#!/usr/bin/env bash
# Cheap gate for the optional WLED renderer. systemd.path starts this unit on
# config changes; disabled installations exit before importing NumPy/Pillow.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="${WLED_CONFIG_FILE:-$PROJECT_DIR/config.json}"

if [ ! -r "$CONFIG_FILE" ] \
        || ! jq --exit-status '.wled.enabled == true' "$CONFIG_FILE" >/dev/null 2>&1; then
    echo "spotify-wled: disabled; renderer remains dormant"
    exit 0
fi

exec "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/wled_sync.py"
