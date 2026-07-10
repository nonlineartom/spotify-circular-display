#!/usr/bin/env bash
# Spotify Pi Display — idempotent Raspberry Pi installer.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$PROJECT_DIR/config.json"
APP_USER="${APP_USER:-${SUDO_USER:-$USER}}"
APP_GROUP="${APP_GROUP:-spotify-display}"
BACKLIGHT_GROUP="spotify-backlight"
DISPLAY_PORT="${DISPLAY_PORT:-5000}"
DISPLAY_BACKEND="${DISPLAY_BACKEND:-chromium}"
ENABLE_GPIO_BUTTONS="${ENABLE_GPIO_BUTTONS:-0}"
INSTALL_RASPOTIFY_FALLBACK="${INSTALL_RASPOTIFY_FALLBACK:-0}"
INSTALL_TEST_DEPS="${INSTALL_TEST_DEPS:-0}"
STAGED_INSTALL="${STAGED_INSTALL:-0}"
TEMP_ROOT="$(mktemp -d)"
CONFIG_TMP=""
cleanup() {
    rm -rf "$TEMP_ROOT"
    [ -z "${CONFIG_TMP:-}" ] || rm -f "$CONFIG_TMP"
}
trap cleanup EXIT

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step() { echo -e "\n${GREEN}▸ $1${NC}"; }
warn() { echo -e "${YELLOW}  $1${NC}"; }
die() { echo -e "${RED}  $1${NC}" >&2; exit 1; }

APP_HOME="$(getent passwd "$APP_USER" 2>/dev/null | cut -d: -f6 || true)"
[ -n "$APP_HOME" ] || die "User '$APP_USER' does not exist; set APP_USER explicitly."
APP_PRIMARY_GROUP="$(id -gn "$APP_USER")"
[ "$APP_USER" != "root" ] \
    || die "Refusing to run the kiosk as root; set APP_USER to the graphical login user."
case "$DISPLAY_PORT" in ''|*[!0-9]*) die "DISPLAY_PORT must be numeric" ;; esac
[ "$DISPLAY_PORT" -ge 1 ] && [ "$DISPLAY_PORT" -le 65535 ] \
    || die "DISPLAY_PORT must be between 1 and 65535"
case "$DISPLAY_BACKEND" in chromium|pygame) ;; *) die "DISPLAY_BACKEND must be chromium or pygame" ;; esac
case "$STAGED_INSTALL" in 0|1) ;; *) die "STAGED_INSTALL must be 0 or 1" ;; esac

step "Installing OS dependencies"
sudo apt-get update -qq
OS_PACKAGES=(
    python3 python3-venv python3-pip python3-dev build-essential swig
    jq curl ca-certificates avahi-daemon alsa-utils
)
if [ "$DISPLAY_BACKEND" = "chromium" ]; then
    CHROMIUM_PACKAGE="chromium"
    apt-cache show chromium >/dev/null 2>&1 || CHROMIUM_PACKAGE="chromium-browser"
    OS_PACKAGES+=("$CHROMIUM_PACKAGE")
fi
if [ "$INSTALL_TEST_DEPS" = "1" ]; then
    OS_PACKAGES+=(nodejs)
fi
sudo apt-get install -y -qq "${OS_PACKAGES[@]}"

step "Creating service groups"
sudo groupadd --system "$APP_GROUP" 2>/dev/null || true
sudo groupadd --system "$BACKLIGHT_GROUP" 2>/dev/null || true
sudo groupadd --system gpio 2>/dev/null || true
SERVICE_GROUPS="$APP_GROUP,$BACKLIGHT_GROUP,gpio"
getent group audio >/dev/null 2>&1 && SERVICE_GROUPS="$SERVICE_GROUPS,audio"
sudo usermod -a -G "$SERVICE_GROUPS" "$APP_USER"

step "Configuring Waveshare backlight access"
UDEV_BACKLIGHT_RULE="$TEMP_ROOT/70-spotify-display-backlight.rules"
printf '%s\n' \
    '# Waveshare 7inch 1080x1080 HDMI Round backlight/touch controller.' \
    '# Raw HID access is limited to the display service dedicated group.' \
    'SUBSYSTEM=="hidraw", KERNEL=="hidraw*", ATTRS{idVendor}=="0712", ATTRS{idProduct}=="000a", GROUP:="spotify-backlight", MODE:="0660"' \
    > "$UDEV_BACKLIGHT_RULE"
sudo install -d -m 0755 /etc/udev/rules.d
sudo install -m 0644 "$UDEV_BACKLIGHT_RULE" \
    /etc/udev/rules.d/70-spotify-display-backlight.rules
if command -v udevadm >/dev/null 2>&1; then
    sudo udevadm control --reload-rules
    sudo udevadm trigger --action=change --subsystem-match=hidraw \
        || warn "Could not retrigger hidraw devices; reconnect Touch USB after setup."
    sudo udevadm settle
fi

step "Installing verified go-librespot receiver"
GO_LIBRESPOT_VERSION="${GO_LIBRESPOT_VERSION:-v0.7.4}"
case "$(uname -m)" in
    aarch64|arm64) GO_LIBRESPOT_ARCH="arm64" ;;
    x86_64|amd64) GO_LIBRESPOT_ARCH="x86_64" ;;
    armv6l|armv7l) GO_LIBRESPOT_ARCH="armv6_rpi" ;;
    *) die "Unsupported architecture for go-librespot: $(uname -m)" ;;
esac
GO_LIBRESPOT_ARCHIVE="go-librespot_linux_${GO_LIBRESPOT_ARCH}.tar.gz"
GO_LIBRESPOT_URL="https://github.com/devgianlu/go-librespot/releases/download/${GO_LIBRESPOT_VERSION}/${GO_LIBRESPOT_ARCHIVE}"
if [ "$GO_LIBRESPOT_VERSION" = "v0.7.4" ]; then
    case "$GO_LIBRESPOT_ARCH" in
        arm64) KNOWN_GO_SHA256="62b6c7ebee6abb1f59fa3cf20a9374dd5e8c5c1ca5dab329dd312f66b59faa8d" ;;
        x86_64) KNOWN_GO_SHA256="ee521eed02100ee4aa9919a147304aed5afc908c5a0c99f515c3da0aed89acf7" ;;
        armv6_rpi) KNOWN_GO_SHA256="3b9c758a5d3802f65beec7da94861bfa27813566cf493e322ab6f4d7d34ca75a" ;;
    esac
else
    KNOWN_GO_SHA256=""
fi
GO_LIBRESPOT_SHA256="${GO_LIBRESPOT_SHA256:-$KNOWN_GO_SHA256}"
[ -n "$GO_LIBRESPOT_SHA256" ] \
    || die "Unknown go-librespot release; supply GO_LIBRESPOT_SHA256 for verification"
curl --fail --location --proto '=https' --tlsv1.2 \
    "$GO_LIBRESPOT_URL" -o "$TEMP_ROOT/$GO_LIBRESPOT_ARCHIVE"
printf '%s  %s\n' "$GO_LIBRESPOT_SHA256" "$TEMP_ROOT/$GO_LIBRESPOT_ARCHIVE" \
    | sha256sum --check --status \
    || die "go-librespot checksum mismatch"
mkdir -p "$TEMP_ROOT/go-librespot"
tar -xzf "$TEMP_ROOT/$GO_LIBRESPOT_ARCHIVE" -C "$TEMP_ROOT/go-librespot"
[ -f "$TEMP_ROOT/go-librespot/go-librespot" ] || die "go-librespot binary missing from archive"
sudo install -m 0755 "$TEMP_ROOT/go-librespot/go-librespot" /usr/local/bin/go-librespot

[ -f "$PROJECT_DIR/go-librespot/config.yml" ] \
    || die "Tracked canonical go-librespot/config.yml is missing"
sudo chown -R "$APP_USER:$APP_GROUP" "$PROJECT_DIR/go-librespot"
sudo chmod 0770 "$PROJECT_DIR/go-librespot"
sudo chmod 0640 "$PROJECT_DIR/go-librespot/config.yml"

if [ "$INSTALL_RASPOTIFY_FALLBACK" = "1" ] && ! command -v raspotify >/dev/null 2>&1; then
    step "Installing optional verified raspotify fallback"
    [ -n "${RASPOTIFY_INSTALLER_SHA256:-}" ] \
        || die "Set RASPOTIFY_INSTALLER_SHA256 to opt into the remote installer"
    RASPOTIFY_INSTALLER_URL="${RASPOTIFY_INSTALLER_URL:-https://dtcooper.github.io/raspotify/install.sh}"
    curl --fail --location --proto '=https' --tlsv1.2 \
        "$RASPOTIFY_INSTALLER_URL" -o "$TEMP_ROOT/raspotify-install.sh"
    printf '%s  %s\n' "$RASPOTIFY_INSTALLER_SHA256" "$TEMP_ROOT/raspotify-install.sh" \
        | sha256sum --check --status || die "raspotify installer checksum mismatch"
    sudo bash "$TEMP_ROOT/raspotify-install.sh"
fi
if id raspotify >/dev/null 2>&1; then
    sudo usermod -a -G "$APP_GROUP" raspotify
fi
if [ "$INSTALL_RASPOTIFY_FALLBACK" = "1" ] && id raspotify >/dev/null 2>&1; then
    sudo install -d -m 0755 /etc/raspotify
    RASPOTIFY_CONF="$TEMP_ROOT/raspotify.conf"
    {
        echo 'LIBRESPOT_NAME="Pi Display"'
        echo 'LIBRESPOT_BITRATE="320"'
        echo 'LIBRESPOT_FORMAT="S16"'
        echo 'LIBRESPOT_INITIAL_VOLUME="80"'
        printf 'LIBRESPOT_ONEVENT="%s/onevent.sh"\n' "$PROJECT_DIR"
    } > "$RASPOTIFY_CONF"
    sudo install -m 0644 "$RASPOTIFY_CONF" /etc/raspotify/conf
else
    warn "Raspotify fallback configuration was not requested; any existing configuration was preserved."
fi

step "Creating pinned Python environment"
[ -f "$PROJECT_DIR/requirements.lock" ] \
    || die "requirements.lock is missing; refusing an unverified dependency install"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10 or newer is required by the locked runtime"
# Recreate instead of reusing: entry-point shebangs embed the absolute venv
# path, and stale packages would otherwise survive a versioned-directory move.
python3 -m venv --clear "$PROJECT_DIR/venv"
PIP_WHEEL="pip-25.1.1-py3-none-any.whl"
PIP_WHEEL_URL="https://files.pythonhosted.org/packages/29/a2/d40fb2460e883eca5199c62cfc2463fd261f760556ae6290f88488c362c0/${PIP_WHEEL}"
PIP_WHEEL_SHA256="2913a38a2abf4ea6b64ab507bd9e967f3b53dc1ede74b01b0931e1ce548751af"
curl --fail --location --proto '=https' --tlsv1.2 \
    "$PIP_WHEEL_URL" -o "$TEMP_ROOT/$PIP_WHEEL"
printf '%s  %s\n' "$PIP_WHEEL_SHA256" "$TEMP_ROOT/$PIP_WHEEL" \
    | sha256sum --check --status || die "pip bootstrap checksum mismatch"
"$PROJECT_DIR/venv/bin/python" -m pip install --no-index \
    "$TEMP_ROOT/$PIP_WHEEL" -q
# rpi-lgpio deliberately replaces the incompatible RPi.GPIO distribution.
"$PROJECT_DIR/venv/bin/python" -m pip uninstall -y RPi.GPIO >/dev/null 2>&1 || true
PYPI=1 "$PROJECT_DIR/venv/bin/python" -m pip install --only-binary=:all: \
    --no-binary=lgpio --require-hashes -r "$PROJECT_DIR/requirements.lock" -q
if [ "$INSTALL_TEST_DEPS" = "1" ]; then
    [ -f "$PROJECT_DIR/requirements-test.lock" ] \
        || die "requirements-test.lock is missing; refusing an unverified test install"
    "$PROJECT_DIR/venv/bin/python" -m pip install --only-binary=:all: \
        --require-hashes -r "$PROJECT_DIR/requirements-test.lock" -q
    warn "Hash-locked test dependencies and Node.js installed for release validation."
fi

step "Configuring Spotify API credentials"
if [ ! -f "$CONFIG" ]; then
    install -m 0600 "$PROJECT_DIR/config.example.json" "$CONFIG"
fi
jq empty "$CONFIG" >/dev/null 2>&1 || die "$CONFIG is not valid JSON; refusing to overwrite it"
CURRENT_CLIENT_ID="$(jq -r '.client_id // empty' "$CONFIG")"
CURRENT_CLIENT_SECRET="$(jq -r '.client_secret // empty' "$CONFIG")"
if [ -z "$CURRENT_CLIENT_ID" ] || [ -z "$CURRENT_CLIENT_SECRET" ] \
        || [ "$CURRENT_CLIENT_ID" = "YOUR_SPOTIFY_CLIENT_ID" ] \
        || [ "$CURRENT_CLIENT_SECRET" = "YOUR_SPOTIFY_CLIENT_SECRET" ]; then
    echo "  Create an app at https://developer.spotify.com/dashboard"
    read -rp "  Client ID:     " CLIENT_ID
    read -rsp "  Client Secret: " CLIENT_SECRET
    echo
    CONFIG_TMP="$(mktemp "$PROJECT_DIR/.config.json.XXXXXX")"
    CLIENT_SECRET_FILE="$TEMP_ROOT/spotify-client-secret"
    (umask 077; printf '%s' "$CLIENT_SECRET" > "$CLIENT_SECRET_FILE")
    jq --arg client_id "$CLIENT_ID" --rawfile client_secret "$CLIENT_SECRET_FILE" \
        '.client_id = $client_id | .client_secret = $client_secret' \
        "$CONFIG" > "$CONFIG_TMP"
    chmod 0600 "$CONFIG_TMP"
    mv -f "$CONFIG_TMP" "$CONFIG"
    CONFIG_TMP=""
    unset CLIENT_SECRET
    rm -f "$CLIENT_SECRET_FILE"
    warn "Credentials merged into config.json with mode 0600."
else
    chmod 0600 "$CONFIG"
    warn "Existing credentials preserved."
fi
unset CURRENT_CLIENT_SECRET

step "Preparing scripts and local data"
chmod +x "$PROJECT_DIR"/{serve.sh,display-launch.sh,wled-launch.sh,onevent.sh,network_watchdog.sh,harden-network.sh}
if [ ! -f "$PROJECT_DIR/idle_playlists.json" ] \
        && [ -f "$PROJECT_DIR/idle_playlists.example.json" ]; then
    install -m 0644 "$PROJECT_DIR/idle_playlists.example.json" "$PROJECT_DIR/idle_playlists.json"
fi
sudo chown "$APP_USER:$APP_GROUP" "$CONFIG"

render_service() {
    local source="$1"
    local destination="$2"
    local rendered="$TEMP_ROOT/$(basename "$source").rendered"
    local escaped_project escaped_project_systemd escaped_home project_systemd
    escaped_project="$(printf '%s' "$PROJECT_DIR" | sed 's/[&|\\]/\\&/g')"
    project_systemd="$PROJECT_DIR"
    project_systemd="${project_systemd//%/%%}"
    project_systemd="${project_systemd//\\/\\x5c}"
    project_systemd="${project_systemd// /\\x20}"
    project_systemd="${project_systemd//$'\t'/\\x09}"
    escaped_project_systemd="$(printf '%s' "$project_systemd" | sed 's/[&|\\]/\\&/g')"
    escaped_home="$(printf '%s' "$APP_HOME" | sed 's/[&|\\]/\\&/g')"
    sed \
        -e "s|@APP_USER@|$APP_USER|g" \
        -e "s|@APP_GROUP@|$APP_GROUP|g" \
        -e "s|@APP_HOME@|$escaped_home|g" \
        -e "s|@PROJECT_DIR_SYSTEMD@|$escaped_project_systemd|g" \
        -e "s|@PROJECT_DIR@|$escaped_project|g" \
        -e "s|@DISPLAY_PORT@|$DISPLAY_PORT|g" \
        "$source" > "$rendered"
    sudo install -m 0644 "$rendered" "$destination"
}

step "Installing hardened system services"
for service in go-librespot spotify-display spotify-buttons spotify-network-watchdog spotify-wled; do
    render_service "$PROJECT_DIR/services/${service}.service" \
        "/etc/systemd/system/${service}.service"
done
render_service "$PROJECT_DIR/services/spotify-wled.path" \
    "/etc/systemd/system/spotify-wled.path"
render_service "$PROJECT_DIR/tmpfiles.d/spotify-display.conf" \
    "/etc/tmpfiles.d/spotify-display.conf"
sudo systemd-tmpfiles --create /etc/tmpfiles.d/spotify-display.conf
sudo systemctl daemon-reload
if ! jq -e '.wled.enabled == true' "$CONFIG" >/dev/null; then
    warn "WLED renderer is dormant; the config path unit activates it after kiosk setup."
fi
if [ "$STAGED_INSTALL" = "1" ]; then
    warn "Staged install: system service enable/disable state was preserved."
else
    # Prepare migration out of the system manager without blanking the live
    # kiosk. Disabling here affects only future boots; the process remains live
    # until the operator's controlled cutover.
    if sudo systemctl list-unit-files spotify-kiosk.service --no-legend 2>/dev/null \
            | grep -q '^spotify-kiosk.service'; then
        sudo systemctl disable spotify-kiosk.service >/dev/null 2>&1 || true
        warn "Legacy system spotify-kiosk remains running until controlled cutover."
    fi
    if [ "$INSTALL_RASPOTIFY_FALLBACK" = "1" ] \
            && id raspotify >/dev/null 2>&1; then
        sudo systemctl disable raspotify.service || true
    fi
    sudo systemctl enable go-librespot spotify-display spotify-network-watchdog \
        spotify-wled spotify-wled.path
    if [ "$ENABLE_GPIO_BUTTONS" = "1" ]; then
        sudo systemctl enable spotify-buttons
    else
        sudo systemctl disable spotify-buttons >/dev/null 2>&1 || true
        warn "GPIO service is opt-in; rerun with ENABLE_GPIO_BUTTONS=1 after wiring buttons."
    fi
fi

step "Installing graphical user service"
USER_SYSTEMD_DIR="$APP_HOME/.config/systemd/user"
sudo install -d -o "$APP_USER" -g "$APP_PRIMARY_GROUP" -m 0755 "$USER_SYSTEMD_DIR"
for service in spotify-kiosk spotify-pygame; do
    rendered="$TEMP_ROOT/${service}.user.service"
    render_service "$PROJECT_DIR/services/${service}.service" "$rendered"
    sudo install -o "$APP_USER" -g "$APP_PRIMARY_GROUP" -m 0644 \
        "$rendered" "$USER_SYSTEMD_DIR/${service}.service"
done
USER_WANTS_DIR="$USER_SYSTEMD_DIR/default.target.wants"
sudo install -d -o "$APP_USER" -g "$APP_PRIMARY_GROUP" -m 0755 \
    "$USER_WANTS_DIR"
if [ "$STAGED_INSTALL" = "1" ]; then
    warn "Staged install: graphical-session service links were preserved."
else
    sudo -u "$APP_USER" rm -f \
        "$USER_WANTS_DIR/spotify-kiosk.service" \
        "$USER_WANTS_DIR/spotify-pygame.service" \
        "$USER_SYSTEMD_DIR/graphical-session.target.wants/spotify-kiosk.service" \
        "$USER_SYSTEMD_DIR/graphical-session.target.wants/spotify-pygame.service"
    USER_DISPLAY_SERVICE="spotify-kiosk.service"
    [ "$DISPLAY_BACKEND" = "pygame" ] && USER_DISPLAY_SERVICE="spotify-pygame.service"
    sudo -u "$APP_USER" ln -s "../$USER_DISPLAY_SERVICE" \
        "$USER_WANTS_DIR/$USER_DISPLAY_SERVICE"
fi
APP_UID="$(id -u "$APP_USER")"
sudo -u "$APP_USER" XDG_RUNTIME_DIR="/run/user/$APP_UID" \
    systemctl --user daemon-reload >/dev/null 2>&1 \
    || warn "User manager is not active yet; it will load the service at next graphical login."

step "Hardening Wi-Fi for unattended recovery"
if [ "$STAGED_INSTALL" = "1" ]; then
    warn "Staged install: Wi-Fi host policy was not changed."
elif command -v nmcli >/dev/null 2>&1; then
    KIOSK_USER="$APP_USER" bash "$PROJECT_DIR/harden-network.sh" \
        || warn "Wi-Fi hardening deferred; re-run ./harden-network.sh after joining Wi-Fi."
else
    warn "NetworkManager is not installed; skipping Wi-Fi hardening."
fi

step "Applying display power policy"
if [ "$STAGED_INSTALL" = "1" ]; then
    warn "Staged install: display host policy was not changed."
else
    sudo raspi-config nonint do_blanking 1 >/dev/null 2>&1 || true
    BOOT_CONFIG=""
    for candidate in /boot/firmware/config.txt /boot/config.txt; do
        [ -f "$candidate" ] && BOOT_CONFIG="$candidate" && break
    done
    if [ -n "$BOOT_CONFIG" ] && grep -q 'vc4-kms-v3d' "$BOOT_CONFIG"; then
        warn "Modern KMS/Wayland detected; legacy hdmi_cvt settings were not written."
        warn "Expose a 1080x1080 mode in the display EDID/compositor if it is not already available."
    elif [ "${APPLY_LEGACY_HDMI_MODE:-0}" = "1" ] && [ -n "$BOOT_CONFIG" ]; then
        sudo cp -n "$BOOT_CONFIG" "${BOOT_CONFIG}.spotify-display.bak" || true
        sudo sed -i '/^hdmi_force_hotplug=/d; /^hdmi_group=/d; /^hdmi_mode=/d; /^hdmi_cvt=/d' "$BOOT_CONFIG"
        LEGACY_BLOCK="$TEMP_ROOT/legacy-hdmi.txt"
        printf '\n# Spotify Pi Display legacy 1080x1080 mode\nhdmi_force_hotplug=1\nhdmi_group=2\nhdmi_mode=87\nhdmi_cvt=1080 1080 60 1 0 0 0\n' > "$LEGACY_BLOCK"
        sudo tee -a "$BOOT_CONFIG" < "$LEGACY_BLOCK" >/dev/null
    else
        warn "Legacy HDMI mode was not changed; set APPLY_LEGACY_HDMI_MODE=1 only on non-KMS images."
    fi
fi

echo
echo -e "${GREEN}Setup complete.${NC}"
echo "  User/path:       $APP_USER / $PROJECT_DIR"
echo "  Server:          http://0.0.0.0:$DISPLAY_PORT (Waitress)"
echo "  Display backend: $DISPLAY_BACKEND (graphical user service)"
echo "  Reboot when ready: sudo reboot"
