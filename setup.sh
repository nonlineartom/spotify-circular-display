#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Spotify Pi Display — One-shot setup script
# Run on the Pi:  cd /home/admin/circle-pi-display && chmod +x setup.sh && ./setup.sh
# ─────────────────────────────────────────────────────────────
set -euo pipefail

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
    curl \
    ca-certificates \
    avahi-daemon \
    alsa-utils

# ── 2. Install Spotify Connect receiver ─────────────────────
step "Installing Raspotify fallback…"
if ! command -v raspotify &>/dev/null; then
    curl -sL https://dtcooper.github.io/raspotify/install.sh | sh
else
    warn "Raspotify already installed, skipping."
fi

step "Configuring Raspotify fallback with onevent handler…"
sudo tee /etc/raspotify/conf > /dev/null <<RASPOTIFY_CONF
LIBRESPOT_NAME="Pi Display"
LIBRESPOT_BITRATE="320"
LIBRESPOT_FORMAT="S16"
LIBRESPOT_INITIAL_VOLUME="80"
LIBRESPOT_QUIET=""
LIBRESPOT_ONEVENT="${PROJECT_DIR}/onevent.sh"
RASPOTIFY_CONF
sudo systemctl disable --now raspotify || true

step "Installing go-librespot primary receiver…"
GO_LIBRESPOT_VERSION="${GO_LIBRESPOT_VERSION:-v0.7.1}"
case "$(uname -m)" in
    aarch64|arm64)
        GO_LIBRESPOT_ARCH="arm64"
        ;;
    x86_64|amd64)
        GO_LIBRESPOT_ARCH="x86_64"
        ;;
    armv6l|armv7l)
        GO_LIBRESPOT_ARCH="armv6_rpi"
        ;;
    *)
        echo "Unsupported architecture for go-librespot: $(uname -m)" >&2
        exit 1
        ;;
esac
GO_LIBRESPOT_ARCHIVE="go-librespot_linux_${GO_LIBRESPOT_ARCH}.tar.gz"
GO_LIBRESPOT_URL="https://github.com/devgianlu/go-librespot/releases/download/${GO_LIBRESPOT_VERSION}/${GO_LIBRESPOT_ARCHIVE}"
TMP_DIR="$(mktemp -d)"
curl -fsSL "$GO_LIBRESPOT_URL" -o "$TMP_DIR/$GO_LIBRESPOT_ARCHIVE"
tar -xzf "$TMP_DIR/$GO_LIBRESPOT_ARCHIVE" -C "$TMP_DIR"
sudo install -m 0755 "$TMP_DIR/go-librespot" /usr/local/bin/go-librespot
rm -rf "$TMP_DIR"

mkdir -p "$PROJECT_DIR/go-librespot"
if [ ! -f "$PROJECT_DIR/go-librespot/config.yml" ]; then
    cat > "$PROJECT_DIR/go-librespot/config.yml" <<'GO_LIBRESPOT_CONF'
log_level: info
device_name: Pi Display
device_type: speaker
audio_backend: alsa
audio_device: default
bitrate: 320
initial_volume: 80
volume_steps: 100
ignore_last_volume: false
zeroconf_enabled: true
zeroconf_port: 0
zeroconf_backend: avahi
credentials:
  type: zeroconf
  zeroconf:
    persist_credentials: false
server:
  enabled: true
  address: 127.0.0.1
  port: 3678
  image_size: large
mpris_enabled: false
GO_LIBRESPOT_CONF
fi

# ── 3. Python virtual environment ───────────────────────────
step "Creating Python virtual environment…"
python3 -m venv "$PROJECT_DIR/venv"
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip -q
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q

# ── 4. Spotify API credentials ──────────────────────────────
step "Spotify API credentials"
echo "  These are used for track metadata lookup (no user login needed)."
echo ""
if [ -z "$(jq -r '.client_id // empty' "$CONFIG" 2>/dev/null)" ]; then
    echo "  Go to https://developer.spotify.com/dashboard to create an app."
    echo ""
    read -rp "  Client ID:     " CLIENT_ID
    read -rp "  Client Secret: " CLIENT_SECRET

    cat > "$CONFIG" <<EOF
{
  "client_id": "${CLIENT_ID}",
  "client_secret": "${CLIENT_SECRET}"
}
EOF
    warn "Credentials saved to config.json"
else
    warn "Credentials already present in config.json, skipping."
fi

# ── 5. Make onevent script executable ────────────────────────
step "Setting up onevent handler…"
chmod +x "$PROJECT_DIR/onevent.sh"
chmod +x "$PROJECT_DIR/network_watchdog.sh"

# ── 6. Install systemd services ─────────────────────────────
step "Installing systemd services…"
for svc in go-librespot spotify-display spotify-buttons spotify-kiosk spotify-network-watchdog; do
    sudo cp "$PROJECT_DIR/services/${svc}.service" /etc/systemd/system/
done
sudo systemctl daemon-reload
sudo systemctl enable go-librespot spotify-display spotify-buttons spotify-kiosk spotify-network-watchdog

# ── 7. Display & desktop config ─────────────────────────────
step "Configuring display (1080x1080, no blanking)…"

# HDMI config
BOOT_CONFIG=""
for f in /boot/firmware/config.txt /boot/config.txt; do
    [ -f "$f" ] && BOOT_CONFIG="$f" && break
done

if [ -n "$BOOT_CONFIG" ]; then
    sudo sed -i '/^hdmi_force_hotplug/d; /^hdmi_group/d; /^hdmi_mode/d; /^hdmi_cvt/d' "$BOOT_CONFIG"
    sudo tee -a "$BOOT_CONFIG" > /dev/null <<'HDMI'

# Spotify Pi Display — 1080x1080 square
hdmi_force_hotplug=1
hdmi_group=2
hdmi_mode=87
hdmi_cvt=1080 1080 60 1 0 0 0
HDMI
    warn "Added HDMI settings to $BOOT_CONFIG"
else
    warn "Could not find boot config — set HDMI manually."
fi

# Disable screen blanking
sudo raspi-config nonint do_blanking 1 2>/dev/null || true

# Auto-hide mouse cursor via unclutter (autostart)
AUTOSTART_DIR="/home/${USER}/.config/lxsession/LXDE-pi"
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

# ── Done ─────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete! Reboot to start everything:${NC}"
echo -e "${GREEN}    sudo reboot${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo "  After reboot, the display services will start automatically:"
echo "    • go-librespot    — Spotify Connect receiver (\"Pi Display\")"
echo "    • spotify-display — Flask server (metadata + web UI)"
echo "    • spotify-kiosk   — Chromium fullscreen on the display"
echo "    • spotify-network-watchdog — restarts Spotify services after Wi-Fi returns"
echo ""
echo "  To use: Open Spotify on your phone → Tap devices → Select \"Pi Display\""
echo "  The display will show album art, lyrics, progress, and local touch controls automatically."
echo "  No QR login or Spotify Web API authentication required for controls."
echo ""
