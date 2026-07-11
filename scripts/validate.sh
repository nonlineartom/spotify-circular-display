#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ -z "${PYTHON_BIN:-}" ]; then
    if [ -x "$ROOT_DIR/venv/bin/python" ]; then
        PYTHON_BIN="$ROOT_DIR/venv/bin/python"
    elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
        PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
    else
        PYTHON_BIN="$(command -v python3)"
    fi
fi
[ -x "$PYTHON_BIN" ] || { echo "Python interpreter not found: $PYTHON_BIN" >&2; exit 1; }

echo "[1/6] Python compilation"
"$PYTHON_BIN" -m compileall -q server.py display.py gpio_buttons.py wled_sync.py scripts tests

echo "[2/6] Python tests"
"$PYTHON_BIN" -m pytest -q

echo "[3/6] Shell syntax"
while IFS= read -r script; do
    bash -n "$script"
done < <(find . \
    \( -path './.git' -o -path './.claude' -o -path './venv' -o -path './.venv' \) \
    -prune -o -name '*.sh' -type f -print | sort)

echo "[4/6] Inline JavaScript syntax"
"$PYTHON_BIN" scripts/check_inline_js.py templates/index.html templates/join.html templates/connect.html

echo "[5/6] Rendered service verification"
SERVICE_RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "$SERVICE_RENDER_DIR"' EXIT
"$PYTHON_BIN" scripts/render_service_templates.py "$SERVICE_RENDER_DIR"
if command -v systemd-analyze >/dev/null 2>&1 \
        && [ "${VERIFY_SYSTEMD:-0}" = "1" ]; then
    systemd-analyze verify "$SERVICE_RENDER_DIR"/*.service "$SERVICE_RENDER_DIR"/*.path
elif command -v systemd-analyze >/dev/null 2>&1; then
    echo "service templates rendered; set VERIFY_SYSTEMD=1 after target dependencies are installed"
else
    echo "service templates rendered; systemd-analyze must also run on the target Pi"
fi

echo "[6/6] LAN HTTPS renderer contract"
"$PYTHON_BIN" scripts/check_lan_https.py

echo "Validation complete"
