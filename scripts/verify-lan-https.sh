#!/usr/bin/env bash
# Verify the active LAN-only HTTPS trust boundary from the Pi itself.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SETTINGS=""
usage() { printf 'Usage: scripts/verify-lan-https.sh --settings FILE\n'; }
die() { printf 'verify-lan-https: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --settings) [ "$#" -ge 2 ] || die "--settings needs a path"; SETTINGS="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[ -n "$SETTINGS" ] || { usage >&2; exit 2; }
[ -f "$SETTINGS" ] || die "settings file not found: $SETTINGS"
[ "$(id -u)" -eq 0 ] || die "run verification as root so nginx and key checks are authoritative"
for command in python3 jq nginx systemctl ss curl openssl; do need "$command"; done

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
python3 "$ROOT_DIR/scripts/render_lan_https.py" --require-files \
    "$SETTINGS" "$TEMP_DIR/expected.conf" >/dev/null

PUBLIC_HOST="$(jq -er '.public_host' "$SETTINGS")"
LAN_ADDRESS="$(jq -er '.lan_listen_address' "$SETTINGS")"
FLASK_PORT="$(jq -er '.flask_port' "$SETTINGS")"
APPLICATION_CONFIG="${SPOTIFY_DISPLAY_CONFIG:-$ROOT_DIR/config.json}"
[ -f "$APPLICATION_CONFIG" ] || die "application config not found: $APPLICATION_CONFIG"
jq -e --arg origin "https://$PUBLIC_HOST" '
    .public_base_url == $origin and .redirect_uri == ($origin + "/callback")
' "$APPLICATION_CONFIG" >/dev/null \
    || die "config.json public_base_url/redirect_uri do not exactly match https://$PUBLIC_HOST"
INSTALLED=/etc/nginx/sites-enabled/spotify-display-lan-https.conf
[ -e "$INSTALLED" ] || die "nginx site is not enabled: $INSTALLED"
cmp -s "$TEMP_DIR/expected.conf" "$INSTALLED" \
    || die "enabled nginx site differs from the rendered, reviewed settings"

systemctl is-active --quiet spotify-display || die "spotify-display is not active"
systemctl is-active --quiet nginx || die "nginx is not active"
nginx -t >/dev/null

BAD_LISTENS="$(nginx -T 2>/dev/null | awk -v expected="$LAN_ADDRESS:443" '
    $1 == "listen" { value=$2; sub(/;$/, "", value); if (value != expected) print value }
')"
[ -z "$BAD_LISTENS" ] \
    || die "nginx has a listener outside $LAN_ADDRESS:443: $BAD_LISTENS"
LISTEN_COUNT="$(nginx -T 2>/dev/null | awk -v expected="$LAN_ADDRESS:443" '
    $1 == "listen" { value=$2; sub(/;$/, "", value); if (value == expected) count++ }
    END { print count + 0 }
')"
[ "$LISTEN_COUNT" -eq 2 ] \
    || die "nginx must contain exactly the two reviewed TLS listeners (found $LISTEN_COUNT)"

mapfile -t TLS_LISTENERS < <(ss -H -ltn 'sport = :443' | awk '{print $4}' | sort -u)
[ "${#TLS_LISTENERS[@]}" -eq 1 ] && [ "${TLS_LISTENERS[0]}" = "$LAN_ADDRESS:443" ] \
    || die "TCP 443 must listen only on $LAN_ADDRESS (found: ${TLS_LISTENERS[*]:-none})"
mapfile -t FLASK_LISTENERS < <(ss -H -ltn "sport = :$FLASK_PORT" | awk '{print $4}' | sort -u)
[ "${#FLASK_LISTENERS[@]}" -eq 1 ] && [ "${FLASK_LISTENERS[0]}" = "127.0.0.1:$FLASK_PORT" ] \
    || die "Flask must listen only on 127.0.0.1:$FLASK_PORT (found: ${FLASK_LISTENERS[*]:-none})"

python3 - "$PUBLIC_HOST" "$LAN_ADDRESS" <<'PY'
import socket
import sys

host, expected = sys.argv[1:]
resolved = {item[4][0] for item in socket.getaddrinfo(host, 443, socket.AF_INET)}
if expected not in resolved:
    raise SystemExit(f"DNS for {host} does not include {expected}: {sorted(resolved)}")
PY

CURL=(curl --silent --show-error --output /dev/null --connect-timeout 3 --max-time 12 \
    --resolve "$PUBLIC_HOST:443:$LAN_ADDRESS")
status() { "${CURL[@]}" --write-out '%{http_code}' "https://$PUBLIC_HOST$1"; }
expect() {
    local path="$1" expected="$2" actual
    actual="$(status "$path")"
    [ "$actual" = "$expected" ] || die "$path returned $actual, expected $expected"
}

# These calls validate the public CA chain and hostname because curl is not
# passed --insecure. The fake valid-shaped pair token proves /pair/ is proxied.
expect /connect 200
expect /join 200
expect /login 401
expect /pair/000000000000 400
CALLBACK_BODY="$(curl --silent --show-error --connect-timeout 3 --max-time 12 \
    --resolve "$PUBLIC_HOST:443:$LAN_ADDRESS" "https://$PUBLIC_HOST/callback")"
printf '%s' "$CALLBACK_BODY" | jq -e '.reason == "no_code"' >/dev/null \
    || die "/callback did not observe the preserved public Host"
expect /static/fonts.css 200
for font in cyrillic-ext cyrillic vietnamese latin-ext latin; do
    expect "/static/fonts/montserrat-$font.woff2" 200
done

expect / 404
expect /api 404
expect /api/health 404
expect /api/auth/status 404
expect /static/mock-album.svg 404
expect /pair/not-a-valid-token 404
POST_STATUS="$("${CURL[@]}" --request POST --write-out '%{http_code}' "https://$PUBLIC_HOST/join")"
[ "$POST_STATUS" = 403 ] || die "POST /join returned $POST_STATUS, expected 403"

if curl --silent --output /dev/null --connect-timeout 2 --max-time 3 \
        "http://$LAN_ADDRESS:$FLASK_PORT/api/health"; then
    die "Flask remains reachable directly through its LAN address"
fi

printf 'LAN HTTPS verification passed for https://%s on %s.\n' "$PUBLIC_HOST" "$LAN_ADDRESS"
printf 'Router/NAT verification remains manual: confirm no 80/443 port-forward and test from mobile data.\n'
