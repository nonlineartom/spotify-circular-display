#!/usr/bin/env bash
# Restart the Spotify display stack when the network comes back.
#
# Spotify Connect/librespot can survive a Wi-Fi drop in a half-connected state:
# the process stays "active", but the device no longer appears in Spotify until
# raspotify is restarted. This watchdog only acts on network state transitions.

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
    systemctl restart raspotify || true
    systemctl restart spotify-display || true
    systemctl try-restart spotify-kiosk || true
}

network_state="unknown"

while true; do
    if has_network; then
        if [ "$network_state" != "up" ]; then
            log "network is up; restarting Spotify display services"
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
