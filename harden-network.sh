#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# harden-network.sh — let the Wi-Fi survive nightly router reboots
# without ever popping the Linux Wi-Fi password dialog over the kiosk.
#
# The dialog you see in the morning is the desktop NetworkManager secret
# agent (on Bookworm/labwc that is wf-panel-pi's network plugin; on older
# images, nm-applet). It pre-fills the saved PSK as masked dots — "lots of
# characters already filled in" — and waits forever for a human, which is
# why only a power cycle clears it. See TROUBLESHOOTING.md for the full
# mechanism.
#
# Idempotent and safe to re-run. No-ops gracefully when there is no Wi-Fi
# connection. Never hardcodes an SSID.
#
# What it does for the active Wi-Fi profile:
#   1. Makes the PSK a SYSTEM-owned secret (psk-flags 0) so NetworkManager
#      answers its own secret requests and never consults a desktop agent —
#      the load-bearing fix; with this set the dialog cannot be raised.
#   2. autoconnect=yes, autoconnect-retries=0 (retry forever), powersave=off
#      so a multi-minute AP outage never exhausts the retry budget or slides
#      into NEED_AUTH.
#   3. Neutralizes any standalone desktop secret agent as defense-in-depth.
#   4. Prints before/after and verifies psk-flags=0.
#
# First-time conversion note: if the PSK is currently agent-owned it lives in
# the login keyring, which a root/SSH run may not be able to read back. If so
# the script will ask you to supply it once and WILL NOT touch the connection
# (so it can never strip a working key):
#     WIFI_PSK='your-wifi-password' ./harden-network.sh
# ─────────────────────────────────────────────────────────────
set -uo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step() { echo -e "\n${GREEN}▸ $1${NC}"; }
warn() { echo -e "${YELLOW}  $1${NC}"; }
err()  { echo -e "${RED}  $1${NC}"; }

KIOSK_USER="${KIOSK_USER:-admin}"
KIOSK_UID="$(id -u "$KIOSK_USER" 2>/dev/null || echo 1000)"

# Use sudo only when not already root, so the script works either way.
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

# ── 0. Preconditions ────────────────────────────────────────
if ! command -v nmcli >/dev/null 2>&1; then
    warn "nmcli not found — this device is not managed by NetworkManager. Nothing to harden."
    exit 0
fi

# ── 1. Detect the active Wi-Fi connection (no SSID hardcoded) ─
step "Detecting active Wi-Fi connection…"
CONN="$(nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null \
        | awk -F: '$2 ~ /wireless/ {print $1; exit}')"
if [ -z "${CONN:-}" ]; then
    # Fall back to the first wifi profile that exists, even if not active.
    CONN="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null \
            | awk -F: '$2 ~ /wireless/ {print $1; exit}')"
fi
if [ -z "${CONN:-}" ]; then
    warn "No Wi-Fi connection profile found (Ethernet-only or not yet configured)."
    warn "Re-run after the Pi has joined Wi-Fi. Nothing to do — exiting cleanly."
    exit 0
fi
warn "Using Wi-Fi connection: \"$CONN\""

# ── 2. Show BEFORE state ────────────────────────────────────
step "Current settings (before):"
nmcli -f 802-11-wireless-security.psk-flags,connection.autoconnect,connection.autoconnect-retries,802-11-wireless.powersave \
    connection show "$CONN" 2>/dev/null | sed 's/^/    /' || true

PSK_FLAGS_BEFORE="$(nmcli -g 802-11-wireless-security.psk-flags connection show "$CONN" 2>/dev/null || echo '')"

# ── 3. Make the PSK system-owned (the load-bearing fix) ─────
# IMPORTANT: when the secret is currently agent-owned the running NM does not
# hold the PSK in its system store, so flipping the flag alone would STRIP the
# key and break Wi-Fi. We only set psk-flags 0 when we can supply the actual
# PSK in the same operation; otherwise we leave the connection untouched.
step "Securing the Wi-Fi PSK as a system-owned secret (psk-flags 0)…"
if [ -z "$PSK_FLAGS_BEFORE" ]; then
    warn "Open network (no PSK) — nothing to convert; skipping secret-ownership step."
elif [ "$PSK_FLAGS_BEFORE" = "0" ]; then
    warn "PSK is already system-owned (psk-flags 0) — no change needed."
else
    # agent-owned (flags 1/2/3): recover the key, then re-supply it while flipping.
    PSK="$(nmcli --show-secrets -g 802-11-wireless-security.psk connection show "$CONN" 2>/dev/null || true)"
    [ -z "$PSK" ] && PSK="${WIFI_PSK:-}"
    if [ -z "$PSK" ]; then
        err "The Wi-Fi key is stored as an agent-owned secret (psk-flags=$PSK_FLAGS_BEFORE) and could"
        err "not be read back automatically (the login keyring may be locked for this session)."
        err "Re-run once with the password so it can be written into the system connection WITHOUT"
        err "breaking Wi-Fi — the connection has been left unchanged:"
        err "    WIFI_PSK='your-wifi-password' $0"
        exit 1
    fi
    if $SUDO nmcli connection modify "$CONN" \
            802-11-wireless-security.psk "$PSK" \
            802-11-wireless-security.psk-flags 0; then
        warn "PSK written into the system connection (psk-flags 0)."
    else
        err "Failed to set psk/psk-flags — leaving the existing secret in place."
        unset PSK
        exit 1
    fi
    unset PSK
fi

# Make it a true system connection (not user/agent scoped).
$SUDO nmcli connection modify "$CONN" connection.permissions '' 2>/dev/null \
    && warn "Connection permissions cleared (system-wide profile)." \
    || warn "Could not clear connection.permissions — non-fatal."

# ── 4. Unattended recovery: autoconnect + infinite retries + no powersave ─
step "Applying unattended-recovery settings…"
$SUDO nmcli connection modify "$CONN" connection.autoconnect yes 2>/dev/null \
    && warn "autoconnect = yes" || warn "Could not set autoconnect — non-fatal."
$SUDO nmcli connection modify "$CONN" connection.autoconnect-retries 0 2>/dev/null \
    && warn "autoconnect-retries = 0 (retry forever)" || warn "Could not set autoconnect-retries — non-fatal."
$SUDO nmcli connection modify "$CONN" 802-11-wireless.powersave 2 2>/dev/null \
    && warn "powersave = 2 (off)" || warn "Could not set powersave — non-fatal."

# ── 5. Neutralize any standalone desktop secret agent (defense-in-depth) ─
# With psk-flags 0 no agent is ever consulted, but if the Wi-Fi is later
# re-edited via the desktop wizard (which can reset psk-flags back to 1) we
# don't want a live agent able to draw a dialog over Chromium.
step "Neutralizing desktop NetworkManager secret agents in the kiosk session…"
AUTOSTART="/home/${KIOSK_USER}/.config/lxsession/LXDE-pi/autostart"
if [ -f "$AUTOSTART" ] && grep -q 'nm-applet' "$AUTOSTART" 2>/dev/null; then
    sed -i '/nm-applet/d' "$AUTOSTART" && warn "Removed nm-applet from lxsession autostart."
fi
$SUDO -u "$KIOSK_USER" XDG_RUNTIME_DIR="/run/user/${KIOSK_UID}" \
    systemctl --user mask nm-applet.service >/dev/null 2>&1 || true
$SUDO -u "$KIOSK_USER" XDG_RUNTIME_DIR="/run/user/${KIOSK_UID}" \
    pkill -x nm-applet >/dev/null 2>&1 || true
$SUDO -u "$KIOSK_USER" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${KIOSK_UID}/bus" \
    gsettings set org.gnome.nm-applet disable-connected-notifications true >/dev/null 2>&1 || true
$SUDO -u "$KIOSK_USER" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${KIOSK_UID}/bus" \
    gsettings set org.gnome.nm-applet disable-disconnected-notifications true >/dev/null 2>&1 || true
warn "Standalone agents masked/killed; notifications silenced where supported."
warn "NOTE: on Bookworm/labwc the panel (wf-panel-pi) can also act as a secret agent."
warn "      With psk-flags 0 it is never consulted; the bulletproof posture is Pi OS"
warn "      Lite + a bare compositor (see TROUBLESHOOTING.md)."

# ── 6. Reactivate so the rewritten secret/settings take effect ─
step "Reactivating the connection so the system secret is migrated…"
$SUDO nmcli connection up "$CONN" >/dev/null 2>&1 \
    && warn "Reactivated \"$CONN\"." \
    || warn "Reactivation deferred (AP may be momentarily unavailable) — settings are saved."

# ── 7. Show AFTER state + verify ────────────────────────────
step "New settings (after):"
nmcli -f 802-11-wireless-security.psk-flags,connection.autoconnect,connection.autoconnect-retries,802-11-wireless.powersave \
    connection show "$CONN" 2>/dev/null | sed 's/^/    /' || true

PSK_FLAGS_AFTER="$(nmcli -g 802-11-wireless-security.psk-flags connection show "$CONN" 2>/dev/null || echo '')"
echo ""
if [ "$PSK_FLAGS_AFTER" = "0" ] || [ -z "$PSK_FLAGS_AFTER" ]; then
    echo -e "${GREEN}  ✓ psk-flags = ${PSK_FLAGS_AFTER:-none/open} — the Wi-Fi password dialog can no longer appear.${NC}"
    exit 0
else
    err "✗ psk-flags = '${PSK_FLAGS_AFTER}' (expected 0). The secret is still agent-owned."
    err "  Re-run with the password:  WIFI_PSK='your-wifi-password' $0"
    exit 1
fi
