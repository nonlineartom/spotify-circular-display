#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Spotify Pi Display — One-shot setup script
# Run on the Pi:  cd /home/pi/spotify-pi && chmod +x setup.sh && ./setup.sh
# Override defaults: DEPLOY_USER=myuser DEPLOY_DIR=/opt/spotify ./setup.sh
# ─────────────────────────────────────────────────────────────
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-$(whoami)}"
DEPLOY_HOME="${DEPLOY_HOME:-$(eval echo ~$DEPLOY_USER)}"
DEPLOY_DIR="${DEPLOY_DIR:-$DEPLOY_HOME/spotify-pi}"

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$PROJECT_DIR/config.json"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

step() { echo -e "\n${GREEN}▸ $1${NC}"; }
warn() { echo -e "${YELLOW}  $1${NC}"; }

# ── 1. System update & dependencies ─────────────────────────
step "Updating system packages…"
sudo apt-get update -qq
sudo apt-get upgrade -y -qq

step "Installing dependencies…"
sudo apt-get install -y -qq \
    python3 python3-venv python3-pip python3-dev \
    chromium-browser \
    unclutter \
    jq \
    curl

# ── 2. Install Raspotify (Spotify Connect) ──────────────────
step "Installing Raspotify…"
if ! command -v raspotify &>/dev/null; then
    curl -sL https://dtcooper.github.io/raspotify/install.sh | sh
else
    warn "Raspotify already installed, skipping."
fi

step "Configuring Raspotify…"
sudo tee /etc/raspotify/conf > /dev/null <<'RASPOTIFY_CONF'
LIBRESPOT_NAME="Pi Display"
LIBRESPOT_BITRATE="320"
LIBRESPOT_FORMAT="S16"
LIBRESPOT_INITIAL_VOLUME="80"
LIBRESPOT_QUIET=""
RASPOTIFY_CONF
sudo systemctl enable raspotify
sudo systemctl restart raspotify

# ── 3. Python virtual environment ───────────────────────────
step "Creating Python virtual environment…"
python3 -m venv "$PROJECT_DIR/venv"
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip -q
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q

# ── 4. Spotify credentials ──────────────────────────────────
step "Spotify API credentials"
if [ -z "$(jq -r '.client_id // empty' "$CONFIG" 2>/dev/null)" ]; then
    echo "  Go to https://developer.spotify.com/dashboard to get these."
    echo ""
    read -rp "  Client ID:     " CLIENT_ID
    read -rp "  Client Secret: " CLIENT_SECRET

    PI_IP=$(hostname -I | awk '{print $1}')
    jq --arg id "$CLIENT_ID" \
       --arg secret "$CLIENT_SECRET" \
       '.client_id = $id | .client_secret = $secret' \
       "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"

    warn "Also add http://${PI_IP}:5000/callback as a Redirect URI in your Spotify app."
else
    warn "Credentials already present in config.json, skipping."
fi

# ── 5. Install systemd services ─────────────────────────────
step "Installing systemd services…"
for svc in spotify-display spotify-buttons spotify-kiosk; do
    sed -e "s|__DEPLOY_DIR__|$DEPLOY_DIR|g" \
        -e "s|__DEPLOY_USER__|$DEPLOY_USER|g" \
        -e "s|__DEPLOY_HOME__|$DEPLOY_HOME|g" \
        "$PROJECT_DIR/services/${svc}.service" \
        | sudo tee /etc/systemd/system/${svc}.service > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable spotify-display spotify-buttons spotify-kiosk

# ── 6. Display & desktop config ─────────────────────────────
step "Configuring display (1080×1080, no blanking)…"

# HDMI config
BOOT_CONFIG=""
for f in /boot/firmware/config.txt /boot/config.txt; do
    [ -f "$f" ] && BOOT_CONFIG="$f" && break
done

if [ -n "$BOOT_CONFIG" ]; then
    # Remove any existing conflicting lines
    sudo sed -i '/^hdmi_force_hotplug/d; /^hdmi_group/d; /^hdmi_mode/d; /^hdmi_cvt/d' "$BOOT_CONFIG"
    # Append display settings
    sudo tee -a "$BOOT_CONFIG" > /dev/null <<'HDMI'

# Spotify Pi Display — 1080×1080 square
hdmi_force_hotplug=1
hdmi_group=2
hdmi_mode=87
hdmi_cvt=1080 1080 60 1 0 0 0
HDMI
    warn "Added HDMI settings to $BOOT_CONFIG"
else
    warn "Could not find boot config — set HDMI manually (see DEPLOYMENT.md)."
fi

# Disable screen blanking
sudo raspi-config nonint do_blanking 1 2>/dev/null || true

# Auto-hide mouse cursor via unclutter (autostart)
AUTOSTART_DIR="$DEPLOY_HOME/.config/lxsession/LXDE-pi"
mkdir -p "$AUTOSTART_DIR"
AUTOSTART_FILE="$AUTOSTART_DIR/autostart"
if ! grep -q "unclutter" "$AUTOSTART_FILE" 2>/dev/null; then
    echo "@unclutter -idle 0.1 -root" >> "$AUTOSTART_FILE"
fi

# Disable screen saver in X
if ! grep -q "xset s off" "$AUTOSTART_FILE" 2>/dev/null; then
    cat >> "$AUTOSTART_FILE" <<'XSET'
@xset s off
@xset -dpms
@xset s noblank
XSET
fi

# ── 7. Initial Spotify authentication ───────────────────────
step "Starting server for initial Spotify authentication…"
"$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/server.py" &
SERVER_PID=$!
sleep 2

PI_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "  ┌──────────────────────────────────────────────────────┐"
echo "  │  Open a browser on any device on your network and go │"
echo "  │  to one of these URLs to log in to Spotify:          │"
echo "  │                                                      │"
echo "  │    http://${PI_IP}:5000/login                        │"
echo "  │    http://$(hostname).local:5000/login               │"
echo "  │                                                      │"
echo "  │  After you see 'Authenticated!', come back here.     │"
echo "  └──────────────────────────────────────────────────────┘"
echo ""
read -rp "  Press Enter after you've authenticated in the browser… "

kill $SERVER_PID 2>/dev/null || true
wait $SERVER_PID 2>/dev/null || true

# Verify token was saved
if [ -n "$(jq -r '.refresh_token // empty' "$CONFIG" 2>/dev/null)" ]; then
    echo -e "  ${GREEN}✓ Authentication successful!${NC}"
else
    warn "⚠ No refresh token found — you may need to re-authenticate after reboot."
    warn "  Visit http://${PI_IP}:5000/login after services start."
fi

# ── Done ─────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete! Reboot to start everything:${NC}"
echo -e "${GREEN}    sudo reboot${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo "  After reboot, four services will start automatically:"
echo "    • raspotify       — Spotify Connect receiver"
echo "    • spotify-display — Flask server (API + web UI)"
echo "    • spotify-buttons — GPIO button monitor"
echo "    • spotify-kiosk   — Chromium fullscreen on the display"
echo ""
echo "  Open Spotify on your phone/computer and select 'Pi Display'"
echo "  as the output device. The display will show album art + controls."
echo ""
