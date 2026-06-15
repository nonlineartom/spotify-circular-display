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

# The Wi-Fi profile name (active OR not). NM keeps the same profile across an
# outage, so cache it once. Never hardcodes an SSID.
WIFI_CONN_CACHE=""
wifi_conn() {
    if [ -z "$WIFI_CONN_CACHE" ] && command -v nmcli >/dev/null 2>&1; then
        WIFI_CONN_CACHE="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null \
            | awk -F: '$2 ~ /wireless/ {print $1; exit}')"
    fi
    printf '%s' "$WIFI_CONN_CACHE"
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
# spotify-kiosk` never clears it, and the device sits on the prompt until a power
# cycle. Kill any standalone agent that drew one (best-effort backstop).
kill_secret_dialogs() {
    pkill -f 'nm-connection-editor' 2>/dev/null || true
    pkill -x 'nm-applet' 2>/dev/null || true
    pkill -f 'polkit-gnome-authentication-agent' 2>/dev/null || true
}

# Force a clean Wi-Fi re-activation. `nmcli connection up` (a) uses the
# system-owned PSK so it never prompts, (b) clears the autoconnect-blocked state
# NM falls into when a mid-association dropout makes it ask an agent for a "new"
# key and none answers — autoconnect-retries=0 does NOT clear that block, only an
# explicit `connection up` does — and (c) makes NM cancel that outstanding secret
# request, which dismisses any dialog an agent had drawn over the kiosk. Bounded
# wait so a still-absent AP just fails fast and we retry next loop.
reconnect_wifi() {
    kill_secret_dialogs
    command -v nmcli >/dev/null 2>&1 || return 0
    local conn
    conn="$(wifi_conn)"
    [ -n "$conn" ] && nmcli -w 25 connection up "$conn" >/dev/null 2>&1 || true
}

network_state="unknown"

while true; do
    if has_network; then
        if [ "$network_state" != "up" ]; then
            log "network is up; clearing any stuck Wi-Fi prompt and restarting Spotify display services"
            kill_secret_dialogs
            restart_spotify_stack
            network_state="up"
        fi
    else
        if [ "$network_state" != "down" ]; then
            log "network is down; marking display idle"
            mark_display_idle
            network_state="down"
        fi
        # While the network is down, keep nudging NetworkManager back up. A
        # nightly router reboot can leave NM blocked on a no-secrets failure
        # (it mis-reads the brief mid-association dropout as a bad key, asks a
        # session agent for a new one, and gives up when none answers). Without
        # this the Pi sits on dead Wi-Fi — and a stray password prompt — until a
        # manual power cycle. reconnect_wifi also dismisses any such prompt.
        reconnect_wifi
    fi

    sleep "$CHECK_INTERVAL"
done
