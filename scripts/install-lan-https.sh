#!/usr/bin/env bash
# Render and, only with --activate, install the LAN-only nginx pairing ingress.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SETTINGS=""
OUTPUT="$ROOT_DIR/build/spotify-display-lan-https.conf"
ACTIVATE=0
SITE_NAME="spotify-display-lan-https.conf"
CHAIN_DIR=""
ROLLBACK_DIR=""
ACTIVATION_STARTED=0
ACTIVATION_COMMITTED=0
NGINX_WAS_ACTIVE=0
NGINX_WAS_ENABLED=0

usage() {
    cat <<'EOF'
Usage: scripts/install-lan-https.sh --settings FILE [--output FILE] [--activate]

Without --activate this validates the pre-provisioned certificate and writes
only a rendered config under build/. --activate is the explicit, root-only
step that installs, enables, tests and reloads nginx on the Pi.
EOF
}

die() { printf 'lan-https: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }
rollback() { :; }
cleanup() {
    local status=$?
    trap - EXIT
    if [ "$ACTIVATION_STARTED" -eq 1 ] && [ "$ACTIVATION_COMMITTED" -eq 0 ]; then
        rollback
    fi
    [ -z "$CHAIN_DIR" ] || rm -rf "$CHAIN_DIR"
    [ -z "$ROLLBACK_DIR" ] || rm -rf "$ROLLBACK_DIR"
    exit "$status"
}
trap cleanup EXIT

while [ "$#" -gt 0 ]; do
    case "$1" in
        --settings) [ "$#" -ge 2 ] || die "--settings needs a path"; SETTINGS="$2"; shift 2 ;;
        --output) [ "$#" -ge 2 ] || die "--output needs a path"; OUTPUT="$2"; shift 2 ;;
        --activate) ACTIVATE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[ -n "$SETTINGS" ] || { usage >&2; exit 2; }
[ -f "$SETTINGS" ] || die "settings file not found: $SETTINGS"
need python3
need jq
need openssl
need stat

python3 "$ROOT_DIR/scripts/render_lan_https.py" \
    --require-files "$SETTINGS" "$OUTPUT"

PUBLIC_HOST="$(jq -er '.public_host' "$SETTINGS")"
LAN_ADDRESS="$(jq -er '.lan_listen_address' "$SETTINGS")"
CERTIFICATE="$(jq -er '.tls_certificate_path' "$SETTINGS")"
PRIVATE_KEY="$(jq -er '.tls_private_key_path' "$SETTINGS")"
FLASK_PORT="$(jq -er '.flask_port' "$SETTINGS")"
APPLICATION_CONFIG="${SPOTIFY_DISPLAY_CONFIG:-$ROOT_DIR/config.json}"
[ -f "$APPLICATION_CONFIG" ] || die "application config not found: $APPLICATION_CONFIG"
jq -e --arg origin "https://$PUBLIC_HOST" '
    .public_base_url == $origin and .redirect_uri == ($origin + "/callback")
' "$APPLICATION_CONFIG" >/dev/null \
    || die "config.json public_base_url/redirect_uri do not exactly match https://$PUBLIC_HOST"

# Fail closed on a stale, wrong-host, encrypted, mismatched or broadly readable
# key. The certificate file must be a leaf followed by any intermediate chain.
openssl x509 -in "$CERTIFICATE" -noout -checkhost "$PUBLIC_HOST" >/dev/null \
    || die "certificate does not cover $PUBLIC_HOST"
openssl x509 -in "$CERTIFICATE" -noout -checkend 604800 >/dev/null \
    || die "certificate expires in less than seven days"
openssl pkey -in "$PRIVATE_KEY" -passin pass: -noout >/dev/null 2>&1 \
    || die "private key is invalid or requires an interactive passphrase"

KEY_MODE="$(stat -Lc '%a' "$PRIVATE_KEY")" \
    || die "could not inspect private-key permissions"
KEY_PERMISSIONS=$((8#$KEY_MODE))
(( (KEY_PERMISSIONS & 0077) == 0 )) \
    || die "private key must not be readable by group or other (mode $KEY_MODE)"

CERT_PUBLIC="$(openssl x509 -in "$CERTIFICATE" -pubkey -noout \
    | openssl pkey -pubin -outform DER 2>/dev/null \
    | openssl dgst -sha256 -r | awk '{print $1}')"
KEY_PUBLIC="$(openssl pkey -in "$PRIVATE_KEY" -passin pass: -pubout -outform DER 2>/dev/null \
    | openssl dgst -sha256 -r | awk '{print $1}')"
[ -n "$CERT_PUBLIC" ] && [ "$CERT_PUBLIC" = "$KEY_PUBLIC" ] \
    || die "certificate and private key do not match"

CHAIN_DIR="$(mktemp -d)"
LEAF="$CHAIN_DIR/leaf.pem"
CHAIN="$CHAIN_DIR/chain.pem"
awk -v leaf="$LEAF" -v chain="$CHAIN" '
    /-----BEGIN CERTIFICATE-----/ { count++ }
    { if (count == 1) print > leaf; else if (count > 1) print > chain }
' "$CERTIFICATE"
[ -s "$LEAF" ] || die "certificate file contains no PEM certificate"
CA_BUNDLE="${CA_BUNDLE:-/etc/ssl/certs/ca-certificates.crt}"
[ -f "$CA_BUNDLE" ] || die "CA bundle not found: $CA_BUNDLE"
if [ -s "$CHAIN" ]; then
    openssl verify -purpose sslserver -CAfile "$CA_BUNDLE" -untrusted "$CHAIN" "$LEAF" >/dev/null \
        || die "certificate chain is not trusted by $CA_BUNDLE"
else
    openssl verify -purpose sslserver -CAfile "$CA_BUNDLE" "$LEAF" >/dev/null \
        || die "certificate is not trusted by $CA_BUNDLE"
fi

if [ "$ACTIVATE" -eq 0 ]; then
    printf 'Rendered and validated: %s\n' "$OUTPUT"
    printf 'No system files or services were changed. Re-run with --activate on the Pi.\n'
    exit 0
fi

[ "$(id -u)" -eq 0 ] || die "--activate must be run as root"
need nginx
need ip
need ss
need systemctl
need curl
systemctl is-active --quiet spotify-display \
    || die "spotify-display must be active before HTTPS activation"
ip -4 -o address show | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$LAN_ADDRESS" \
    || die "$LAN_ADDRESS is not assigned to this host"

AVAILABLE_DIR=/etc/nginx/sites-available
ENABLED_DIR=/etc/nginx/sites-enabled
[ -d "$AVAILABLE_DIR" ] && [ -d "$ENABLED_DIR" ] \
    || die "Debian-style nginx sites-available/sites-enabled directories are required"
DESTINATION="$AVAILABLE_DIR/$SITE_NAME"
ENABLED="$ENABLED_DIR/$SITE_NAME"

# This appliance configuration owns nginx. Refuse to coexist with another
# enabled listener; the stock HTTP default is disabled transactionally below.
for candidate in "$ENABLED_DIR"/* /etc/nginx/conf.d/*.conf; do
    [ -e "$candidate" ] || continue
    [ "$candidate" = "$ENABLED" ] && continue
    [ "$candidate" = "$ENABLED_DIR/default" ] && continue
    if grep -Eq '^[[:space:]]*listen[[:space:]]' "$candidate"; then
        die "another enabled nginx listener must be removed deliberately: $candidate"
    fi
done

ROLLBACK_DIR="$(mktemp -d)"
HAD_DESTINATION=0
HAD_ENABLED=0
HAD_DEFAULT=0
DROPIN_DIR=/etc/systemd/system/spotify-display.service.d
DROPIN="$DROPIN_DIR/lan-https-loopback.conf"
HAD_DROPIN=0
[ ! -e "$DESTINATION" ] || { cp -a "$DESTINATION" "$ROLLBACK_DIR/site"; HAD_DESTINATION=1; }
[ ! -e "$ENABLED" ] || { cp -a "$ENABLED" "$ROLLBACK_DIR/enabled"; HAD_ENABLED=1; }
[ ! -e "$ENABLED_DIR/default" ] || { cp -a "$ENABLED_DIR/default" "$ROLLBACK_DIR/default"; HAD_DEFAULT=1; }
[ ! -e "$DROPIN" ] || { cp -a "$DROPIN" "$ROLLBACK_DIR/dropin"; HAD_DROPIN=1; }
systemctl is-active --quiet nginx && NGINX_WAS_ACTIVE=1 || true
systemctl is-enabled --quiet nginx && NGINX_WAS_ENABLED=1 || true

rollback() {
    set +e
    printf 'lan-https: activation failed; restoring previous nginx and Flask bindings\n' >&2
    rm -f "$ENABLED" "$DESTINATION" "$ENABLED_DIR/default"
    [ "$HAD_DESTINATION" -eq 0 ] || cp -a "$ROLLBACK_DIR/site" "$DESTINATION"
    [ "$HAD_ENABLED" -eq 0 ] || cp -a "$ROLLBACK_DIR/enabled" "$ENABLED"
    [ "$HAD_DEFAULT" -eq 0 ] || cp -a "$ROLLBACK_DIR/default" "$ENABLED_DIR/default"
    rm -f "$DROPIN"
    [ "$HAD_DROPIN" -eq 0 ] || cp -a "$ROLLBACK_DIR/dropin" "$DROPIN"
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl restart spotify-display >/dev/null 2>&1 || true
    if [ "$NGINX_WAS_ACTIVE" -eq 1 ]; then
        nginx -t >/dev/null 2>&1 && systemctl reload nginx >/dev/null 2>&1 || true
    else
        systemctl stop nginx >/dev/null 2>&1 || true
    fi
    [ "$NGINX_WAS_ENABLED" -eq 1 ] || systemctl disable nginx >/dev/null 2>&1 || true
}

ACTIVATION_STARTED=1
install -d -m 0755 "$DROPIN_DIR"
install -m 0644 "$ROOT_DIR/deploy/systemd/spotify-display-loopback.conf" "$DROPIN"
systemctl daemon-reload
if ! systemctl restart spotify-display; then
    die "spotify-display failed after binding Waitress to loopback; previous files restored"
fi
FLASK_LISTENERS=()
for _attempt in {1..20}; do
    mapfile -t FLASK_LISTENERS < <(ss -H -ltn "sport = :$FLASK_PORT" | awk '{print $4}' | sort -u)
    if [ "${#FLASK_LISTENERS[@]}" -eq 1 ] \
            && [ "${FLASK_LISTENERS[0]:-}" = "127.0.0.1:$FLASK_PORT" ]; then
        break
    fi
    sleep 0.5
done
if [ "${#FLASK_LISTENERS[@]}" -ne 1 ] \
        || [ "${FLASK_LISTENERS[0]:-}" != "127.0.0.1:$FLASK_PORT" ]; then
    die "Flask did not bind exclusively to 127.0.0.1:$FLASK_PORT"
fi
FLASK_HEALTHY=0
for _attempt in {1..20}; do
    if curl --silent --fail --max-time 2 \
            "http://127.0.0.1:$FLASK_PORT/api/health" >/dev/null; then
        FLASK_HEALTHY=1
        break
    fi
    sleep 0.5
done
[ "$FLASK_HEALTHY" -eq 1 ] || die "Flask loopback health check did not become ready"

rm -f "$ENABLED_DIR/default"
install -m 0644 "$OUTPUT" "$DESTINATION"
ln -sfn "$DESTINATION" "$ENABLED"
if ! nginx -t; then
    die "nginx rejected the rendered configuration; previous files restored"
fi
BAD_LISTENS="$(nginx -T 2>/dev/null | awk -v expected="$LAN_ADDRESS:443" '
    $1 == "listen" { value=$2; sub(/;$/, "", value); if (value != expected) print value }
')"
[ -z "$BAD_LISTENS" ] \
    || die "nginx contains a listener outside $LAN_ADDRESS:443: $BAD_LISTENS"
LISTEN_COUNT="$(nginx -T 2>/dev/null | awk -v expected="$LAN_ADDRESS:443" '
    $1 == "listen" { value=$2; sub(/;$/, "", value); if (value == expected) count++ }
    END { print count + 0 }
')"
[ "$LISTEN_COUNT" -eq 2 ] \
    || die "nginx must contain exactly the two reviewed TLS listeners (found $LISTEN_COUNT)"
if ! systemctl enable --now nginx; then
    die "nginx failed to start; previous files restored"
fi
if ! systemctl reload nginx; then
    die "nginx failed to reload; previous files restored"
fi
ACTIVATION_COMMITTED=1
printf 'Activated %s on https://%s/ (LAN %s only).\n' "$SITE_NAME" "$PUBLIC_HOST" "$LAN_ADDRESS"
printf 'Run scripts/verify-lan-https.sh --settings %s from the Pi.\n' "$SETTINGS"
