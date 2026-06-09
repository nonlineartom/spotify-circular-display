#!/usr/bin/env bash
# Restart the Spotify display stack when the network comes back.
#
# Spotify Connect receivers can survive a Wi-Fi drop in a half-connected state:
# the process stays "active", but the device no longer appears in Spotify until
# the receiver is restarted. This watchdog only acts on network transitions.

set -u

CHECK_INTERVAL="${CHECK_INTERVAL:-20}"
STATE_FILE="${STATE_FILE:-/tmp/spotify-state.json}"
ROUTE_TARGET="${ROUTE_TARGET:-1.1.1.1}"

log() {
    echo "spotify-network-watchdog: $*"
}

has_network() {
    ip route get "$ROUTE_TARGET" >/dev/null 2>&1
}

mark_display_idle() {
    local tmp_file
    tmp_file="${STATE_FILE}.tmp"
    printf '{"event":"network_down","timestamp":%s,"is_playing":false}\n' "$(date +%s)" > "$tmp_file"
    chmod 0644 "$tmp_file"
    mv "$tmp_file" "$STATE_FILE"
}

restart_spotify_stack() {
    if systemctl list-unit-files go-librespot.service --no-legend 2>/dev/null | grep -q '^go-librespot.service'; then
        systemctl restart go-librespot || true
    else
        systemctl restart raspotify || true
    fi
    systemctl restart spotify-display || true
    systemctl try-restart spotify-kiosk || true
}

# A stray NetworkManager secret-agent dialog — the Wi-Fi password box pre-filled
# with masked dots — survives a kiosk restart because it belongs to a different
# process (the desktop panel / nm-applet), not Chromium. So `try-restart
# spotify-kiosk` above never clears it, and the device sits on the prompt until a
# power cycle. On network recovery, close any such dialog and force a clean
# re-activation so NM stops waiting on an outstanding secret request. With
# harden-network.sh applied (psk-flags 0) this never fires; it rescues units that
# were deployed before the hardening, without a power cycle.
dismiss_wifi_prompt() {
    pkill -f 'nm-connection-editor' 2>/dev/null || true
    pkill -x 'nm-applet' 2>/dev/null || true
    pkill -f 'polkit-gnome-authentication-agent' 2>/dev/null || true
    command -v nmcli >/dev/null 2>&1 || return 0
    local conn
    conn="$(nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null \
            | awk -F: '$2 ~ /wireless/ {print $1; exit}')"
    [ -n "$conn" ] && nmcli connection up "$conn" >/dev/null 2>&1 || true
}

network_state="unknown"

while true; do
    if has_network; then
        if [ "$network_state" != "up" ]; then
            log "network is up; clearing any stuck Wi-Fi prompt and restarting Spotify display services"
            dismiss_wifi_prompt
            restart_spotify_stack
            network_state="up"
        fi
    else
        if [ "$network_state" != "down" ]; then
            log "network is down; marking display idle"
            mark_display_idle
            network_state="down"
        fi
    fi

    sleep "$CHECK_INTERVAL"
done
