#!/usr/bin/env bash
# Debounced network-transition recovery for the Spotify receiver.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECK_INTERVAL="${CHECK_INTERVAL:-20}"
TRANSITION_SAMPLES="${TRANSITION_SAMPLES:-2}"
ROUTE_TARGET="${ROUTE_TARGET:-1.1.1.1}"
KIOSK_USER="${KIOSK_USER:-${SUDO_USER:-admin}}"
export SPOTIFY_STATE_FILE="${SPOTIFY_STATE_FILE:-/run/spotify-display/spotify-state.json}"

log() {
    echo "spotify-network-watchdog: $*"
}

has_network() {
    ip route get "$ROUTE_TARGET" 2>/dev/null | grep -qE 'dev [^ ]+'
}

wifi_device() {
    command -v nmcli >/dev/null 2>&1 || return 1
    local route_device
    route_device="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
    if [ -n "$route_device" ] \
            && nmcli -t -f DEVICE,TYPE device status 2>/dev/null \
                | awk -F: -v device="$route_device" \
                    '$1 == device && $2 == "wifi" {found=1} END {exit !found}'; then
        printf '%s' "$route_device"
        return 0
    fi
    nmcli -t -f DEVICE,TYPE device status 2>/dev/null \
        | awk -F: '$2 == "wifi" {print $1; exit}'
}

clear_user_wifi_prompts() {
    local uid
    uid="$(id -u "$KIOSK_USER" 2>/dev/null || true)"
    [ -n "$uid" ] || return 0
    # Scope cleanup to the kiosk user and known NetworkManager UI processes.
    pkill -u "$uid" -x nm-connection-editor 2>/dev/null || true
    pkill -u "$uid" -x nm-applet 2>/dev/null || true
}

reconnect_wifi() {
    command -v nmcli >/dev/null 2>&1 || return 0
    local device
    device="$(wifi_device || true)"
    [ -n "$device" ] || return 0
    clear_user_wifi_prompts
    nmcli -w 12 device connect "$device" >/dev/null 2>&1 || true
}

mark_display_idle() {
    PLAYER_EVENT=network_down TRACK_ID='' DURATION_MS='' POSITION_MS='' VOLUME='' \
        "$SCRIPT_DIR/onevent.sh" \
        || log "warning: could not publish network-down playback state"
}

go_librespot_healthy() {
    systemctl is-active --quiet go-librespot.service \
        && curl --silent --show-error --fail --max-time 2 \
            http://127.0.0.1:3678/status >/dev/null 2>&1
}

recover_receiver() {
    if systemctl list-unit-files go-librespot.service --no-legend 2>/dev/null \
            | grep -q '^go-librespot.service'; then
        log "network recovered; restarting go-librespot"
        systemctl restart go-librespot.service || true
        local attempt
        for attempt in 1 2 3 4 5; do
            if go_librespot_healthy; then
                systemctl stop raspotify.service >/dev/null 2>&1 || true
                log "go-librespot is healthy"
                return 0
            fi
            sleep 2
        done
        log "go-librespot failed its local health check"
    fi

    # Legacy fallback is based on receiver health, not mere unit-file presence.
    if systemctl list-unit-files raspotify.service --no-legend 2>/dev/null \
            | grep -q '^raspotify.service'; then
        log "starting legacy raspotify fallback"
        systemctl stop go-librespot.service >/dev/null 2>&1 || true
        systemctl restart raspotify.service || true
    else
        log "no healthy Spotify receiver and no raspotify fallback is installed"
        return 1
    fi
}

if has_network; then
    network_state="up"
else
    network_state="down"
    mark_display_idle
fi
candidate_state="$network_state"
candidate_count=0
log "initial network state=$network_state; no boot-time restart performed"

while true; do
    if has_network; then
        observed="up"
    else
        observed="down"
    fi

    if [ "$observed" = "$candidate_state" ]; then
        candidate_count=$((candidate_count + 1))
    else
        candidate_state="$observed"
        candidate_count=1
    fi

    if [ "$observed" = "down" ]; then
        reconnect_wifi
    fi

    if [ "$observed" != "$network_state" ] \
            && [ "$candidate_count" -ge "$TRANSITION_SAMPLES" ]; then
        network_state="$observed"
        if [ "$network_state" = "down" ]; then
            log "network-down transition confirmed; marking display idle"
            mark_display_idle
        else
            clear_user_wifi_prompts
            recover_receiver || true
        fi
    fi

    sleep "$CHECK_INTERVAL"
done
