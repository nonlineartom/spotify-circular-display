# Raspberry Pi 5 staged deployment, acceptance and rollback

This guide upgrades the remediated branch without destroying the known-good
installation. The repository has passed host validation and 1080×1080 browser
fixture checks. SSH to the live target is available as `admin@pi5.local` when
the dedicated `~/.ssh/id_ed25519_circle_pi` identity is selected explicitly.

The pre-release inspection returned HTTP 200 with go-librespot available, while
the `Server` header still identified Werkzeug and fallback state still pointed
at `/tmp/spotify-state.json`. The live checkout is also dirty with the original
LCD-dimming trial. Do not pull, reset or copy over it: preserve it as the
known-good installation and deploy a pinned commit into a new directory.

Do not copy files over the running directory and reboot blindly. Use a versioned
release, retain config/service backups, and do not declare success until the
hardware acceptance table is complete.

## 1. Record the current appliance

Run these on the Pi before changing anything. Replace the path/user if needed.

```bash
umask 077
export OLD_RELEASE=/home/admin/circle-pi-display
export RELEASE_ID=remediation-$(date +%Y%m%d-%H%M%S)
export BACKUP_DIR=/home/admin/spotify-display-backups/$RELEASE_ID
mkdir -p "$BACKUP_DIR/system" "$BACKUP_DIR/user" "$BACKUP_DIR/bin" \
  "$BACKUP_DIR/host"

cp -a "$OLD_RELEASE/config.json" "$BACKUP_DIR/config.json"
cp -a "$OLD_RELEASE/go-librespot/config.yml" "$BACKUP_DIR/go-librespot-config.yml"
[ ! -e "$OLD_RELEASE/go-librespot/credentials.json" ] || \
  cp -a "$OLD_RELEASE/go-librespot/credentials.json" \
    "$BACKUP_DIR/go-librespot-credentials.json"
for unit in go-librespot.service spotify-display.service \
  spotify-network-watchdog.service spotify-wled.service spotify-wled.path \
  spotify-buttons.service spotify-kiosk.service; do
  [ ! -e "/etc/systemd/system/$unit" ] || \
    sudo cp -a "/etc/systemd/system/$unit" "$BACKUP_DIR/system/"
done
for unit in spotify-kiosk.service spotify-pygame.service; do
  [ ! -e "$HOME/.config/systemd/user/$unit" ] || \
    cp -a "$HOME/.config/systemd/user/$unit" "$BACKUP_DIR/user/"
done
[ ! -x /usr/local/bin/go-librespot ] || \
  sudo cp -a /usr/local/bin/go-librespot "$BACKUP_DIR/bin/"
[ ! -e /etc/raspotify/conf ] || \
  sudo cp -a /etc/raspotify/conf "$BACKUP_DIR/host/raspotify.conf"
[ ! -e /etc/tmpfiles.d/spotify-display.conf ] || \
  sudo cp -a /etc/tmpfiles.d/spotify-display.conf "$BACKUP_DIR/host/"
[ ! -e /etc/udev/rules.d/70-spotify-display-backlight.rules ] || \
  sudo cp -a /etc/udev/rules.d/70-spotify-display-backlight.rules \
    "$BACKUP_DIR/host/"
[ ! -d "$HOME/.config/systemd/user/graphical-session.target.wants" ] || \
  cp -a "$HOME/.config/systemd/user/graphical-session.target.wants" \
    "$BACKUP_DIR/user/"
for desktop_path in "$HOME/.config/labwc" "$HOME/.config/wayfire.ini" \
  "$HOME/.config/lxsession"; do
  [ ! -e "$desktop_path" ] || cp -a "$desktop_path" "$BACKUP_DIR/host/"
done

printf 'scope\tunit\tactive\tenabled\n' > "$BACKUP_DIR/unit-state.tsv"
for unit in go-librespot spotify-display spotify-network-watchdog \
  spotify-wled spotify-wled.path spotify-buttons spotify-kiosk raspotify; do
  printf 'system\t%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-active "$unit" 2>/dev/null || true)" \
    "$(systemctl is-enabled "$unit" 2>/dev/null || true)" \
    >> "$BACKUP_DIR/unit-state.tsv"
done
for unit in spotify-kiosk spotify-pygame; do
  printf 'user\t%s\t%s\t%s\n' "$unit" \
    "$(systemctl --user is-active "$unit" 2>/dev/null || true)" \
    "$(systemctl --user is-enabled "$unit" 2>/dev/null || true)" \
    >> "$BACKUP_DIR/unit-state.tsv"
done

systemctl --version > "$BACKUP_DIR/systemd-version.txt"
python3 --version > "$BACKUP_DIR/python-version.txt"
uname -a > "$BACKUP_DIR/uname.txt"
sudo systemctl status go-librespot spotify-display spotify-wled \
  --no-pager > "$BACKUP_DIR/service-status.txt" 2>&1 || true
systemctl list-unit-files 'go-librespot*' 'raspotify*' 'spotify-*' \
  > "$BACKUP_DIR/unit-files.txt"
sudo journalctl -u go-librespot -u spotify-display -u spotify-wled \
  -n 300 --no-pager > "$BACKUP_DIR/journal.txt"
curl -fsS http://127.0.0.1:5000/api/health \
  > "$BACKUP_DIR/health.json" || true
nmcli -f NAME,UUID,TYPE,DEVICE,AUTOCONNECT connection show \
  > "$BACKUP_DIR/networkmanager.txt" 2>&1 || true
aplay -l > "$BACKUP_DIR/alsa.txt" 2>&1 || true
(wlr-randr 2>/dev/null || xrandr) > "$BACKUP_DIR/display.txt" 2>&1 || true
lsusb -d 0712:000a > "$BACKUP_DIR/backlight-usb.txt" 2>&1 || true
ls -l /dev/hidraw* > "$BACKUP_DIR/hidraw.txt" 2>&1 || true
vcgencmd measure_temp > "$BACKUP_DIR/temperature.txt" 2>&1 || true
vcgencmd get_throttled > "$BACKUP_DIR/throttled.txt" 2>&1 || true
dmesg | tail -n 500 > "$BACKUP_DIR/dmesg.txt" 2>&1 || true
```

Protect the backup; it contains Spotify application/refresh credentials:

```bash
chmod 700 "$BACKUP_DIR"
chmod 600 "$BACKUP_DIR/config.json" 2>/dev/null || true
chmod 600 "$BACKUP_DIR/go-librespot-credentials.json" 2>/dev/null || true
```

Also record the active Wi-Fi connection name, audio sink, display mode, USB HID
identity and any configured WLED addresses:

```bash
nmcli -t -f NAME,TYPE,DEVICE connection show --active
aplay -l
wlr-randr 2>/dev/null || xrandr
lsusb -d 0712:000a
ls -l /dev/hidraw*
```

## 2. Prepare a versioned candidate

Clone/copy this branch to a new directory, never over `$OLD_RELEASE`:

```bash
export NEW_RELEASE=/home/admin/spotify-circular-display-remediation
export RELEASE_SHA='<pushed-commit-sha>'
git clone <repository-url> "$NEW_RELEASE"
cd "$NEW_RELEASE"
git fetch origin codex/multiphase-remediation
git checkout --detach "$RELEASE_SHA"
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
jq -e 'type == "object"' "$BACKUP_DIR/config.json"
jq -s '.[0] * .[1]' config.example.json "$BACKUP_DIR/config.json" \
  > config.json.new
install -m 0600 config.json.new config.json
rm config.json.new
[ ! -e "$BACKUP_DIR/go-librespot-credentials.json" ] || \
  install -m 0600 "$BACKUP_DIR/go-librespot-credentials.json" \
    go-librespot/credentials.json
cp -a "$BACKUP_DIR/go-librespot-config.yml" go-librespot/config.live.yml
diff -u go-librespot/config.yml go-librespot/config.live.yml || true
```

The recursive `jq` merge adds new defaults while live values win. Review the
go-librespot diff and deliberately carry over any target-specific audio device
or name; retain the candidate loopback status server on port 3678. At minimum
also review:

- `public_base_url`, `redirect_uri`, `guest_session_hours` and
  `security.owner_token`;
- `allow_web_api_control_fallback` (recommended `false`) and the optional
  targeted legacy device ID;
- `backlight` safe maximum, idle level and ramp settings;
- each WLED device's `pixel_count`, reverse, phase, brightness and gamma.

If the current `config.json` is malformed, stop. The new server deliberately
refuses to overwrite it; repair a copy offline and preserve the original bytes.

## 3. Inspect and render before system changes

The exact candidate commit must already have a green repository/CI release
gate. Before installing its dependencies or units on the Pi, perform the checks
that need only the system Python and Bash:

```bash
python3 -m compileall -q server.py display.py gpio_buttons.py wled_sync.py scripts tests
while IFS= read -r script; do bash -n "$script"; done < <(find . \
  \( -path './.git' -o -path './.claude' -o -path './venv' -o -path './.venv' \) \
  -prune -o -name '*.sh' -type f -print | sort)
git diff --check
```

Render units for the real target values and ask systemd to parse them before
installing:

```bash
APP_USER="$(id -un)"
APP_GROUP=spotify-display
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
RENDERED="$(mktemp -d)"
python3 scripts/render_service_templates.py "$RENDERED" \
  --app-user "$APP_USER" \
  --app-group "$APP_GROUP" \
  --app-home "$APP_HOME" \
  --project-dir "$NEW_RELEASE" \
  --display-port 5000
sudo systemd-analyze verify \
  "$RENDERED/go-librespot.service" \
  "$RENDERED/spotify-display.service" \
  "$RENDERED/spotify-network-watchdog.service" \
  "$RENDERED/spotify-wled.service" \
  "$RENDERED/spotify-wled.path" \
  "$RENDERED/spotify-buttons.service"
systemd-analyze --user verify \
  "$RENDERED/spotify-kiosk.service" \
  "$RENDERED/spotify-pygame.service"
rm -rf "$RENDERED"
```

Resolve every syntax, path, executable, user/group or dependency error before
continuing. Warnings about units absent only from the render directory should be
cross-checked against the installed Pi system, not ignored wholesale.

## 4. Install without rebooting first

The installer verifies go-librespot/pip artifacts, creates the locked virtual
environment, merges rather than replaces existing credentials, renders units
for the actual path/user and installs tmpfiles/udev policy. `STAGED_INSTALL=1`
does not change current or next-boot service selection, user graphical-session
wants, Wi-Fi or display power policy. The known-good receiver, kiosk and network
therefore remain untouched until cutover.

```bash
cd "$NEW_RELEASE"
APP_USER="$(id -un)" DISPLAY_PORT=5000 DISPLAY_BACKEND=chromium \
  INSTALL_TEST_DEPS=1 STAGED_INSTALL=1 ./setup.sh
```

The test flag installs the separately hash-locked pytest toolchain and Node.js;
neither is loaded by the running appliance. Now execute the complete release
gate through the newly created candidate environment:

```bash
bash scripts/validate.sh
```

The script automatically selects `venv/bin/python`. A minimal production image
may omit test dependencies only after this exact release/target combination has
already been validated elsewhere.

Only use the optional flags when their hardware/legacy dependency is present:

```bash
ENABLE_GPIO_BUTTONS=1 ./setup.sh
DISPLAY_BACKEND=pygame ./setup.sh
INSTALL_RASPOTIFY_FALLBACK=1 \
  RASPOTIFY_INSTALLER_SHA256='<reviewed checksum>' ./setup.sh
```

Re-run verification against the installed units:

```bash
sudo systemd-analyze verify \
  /etc/systemd/system/go-librespot.service \
  /etc/systemd/system/spotify-display.service \
  /etc/systemd/system/spotify-network-watchdog.service \
  /etc/systemd/system/spotify-wled.service \
  /etc/systemd/system/spotify-wled.path \
  /etc/systemd/system/spotify-buttons.service
systemd-analyze --user verify \
  "$HOME/.config/systemd/user/spotify-kiosk.service" \
  "$HOME/.config/systemd/user/spotify-pygame.service"
```

Confirm config ownership before starting the candidate:

```bash
stat -c '%a %U %G %n' config.json
getent group spotify-display
getent group spotify-backlight
id
```

A new supplementary-group membership may require logout/login or reboot before
the user service can access GPIO/HID.

## 5. Controlled cutover

Stop the graphical user service, then the affected system services. Do not kill
the desktop or NetworkManager.

```bash
systemctl --user stop spotify-kiosk spotify-pygame 2>/dev/null || true
sudo systemctl disable --now spotify-kiosk.service 2>/dev/null || true
sudo systemctl stop spotify-network-watchdog spotify-wled.path \
  spotify-buttons spotify-wled spotify-display go-librespot raspotify
sudo systemctl daemon-reload
systemctl --user daemon-reload

sudo systemctl disable raspotify.service 2>/dev/null || true
sudo systemctl enable go-librespot spotify-display spotify-network-watchdog \
  spotify-wled.path
systemctl --user enable spotify-kiosk

sudo systemctl start go-librespot
curl --retry 15 --retry-delay 1 --retry-connrefused \
  -fsS http://127.0.0.1:3678/status | jq

sudo systemctl start spotify-display
curl --retry 15 --retry-delay 1 --retry-connrefused \
  -fsS http://127.0.0.1:5000/api/health | jq

sudo systemctl start spotify-wled.path
if jq -e '.wled.enabled == true' config.json >/dev/null; then
  sudo systemctl enable --now spotify-wled
else
  sudo systemctl disable spotify-wled 2>/dev/null || true
fi
sudo systemctl start spotify-network-watchdog
systemctl --user start spotify-kiosk
```

The path unit alone does not guarantee an initial WLED start, so an enabled
renderer is started explicitly. Start `spotify-buttons` only after verifying
wiring and opt-in. Apply `harden-network.sh` or host display policy separately
only after the core stack is healthy and their recorded before/after state has
been reviewed.

## 6. Acceptance checklist

Record pass/fail, evidence and temperature for every row. A repository test is
not a substitute for the physical checks.

| Area | Acceptance test |
|---|---|
| Boot/readiness | Cold boot reaches the square kiosk without a fixed-delay race; no service is crash-looping. |
| Spotify Connect | “Pi Display” appears from two LAN clients; connect, play, pause, resume, next and same-track previous all work. |
| Motion | At 1080×1080, the record reaches stable 33⅓/45 motion, pause returns cleanly to zero, rapid skips do not flash old art, and no-art uses the neutral sleeve. |
| Static/dim | Paused/idle/dim scenes reduce frame activity; first touch wakes without firing the underlying control. |
| Gestures | Single swipe/tap, two-finger seek/volume/pinch and three-finger brightness all complete; pointer cancellation sends no action. |
| Crate/tracklist/lyrics | Owner/private data is visible only in an owner context; empty account clears old cards; modal keyboard/focus/Escape behaviour is correct. |
| Offline/error | Restart go-librespot and briefly remove the route; UI shows continuity/error rather than false idle, then recovers without a stale transition. |
| Backlight | Confirm the correct `0712:000a` hidraw interface, 10% first-contact command, stepped ramp, idle/wake and rediscovery after a controlled USB reconnect. |
| Power | Watch `dmesg` and `vcgencmd get_throttled`; 80% physical brightness must not cause USB reset, touch loss or undervoltage. |
| WLED | Verify pixel count/direction/phase/gamma per strip, smooth 4-second pause ramp, 8-second transport grace, idle realtime release and status file. |
| GPIO | When opted in, every BCM button produces one local API action and the non-root unit remains stable. |
| Network | Temporarily disable/re-enable the AP or route; no password dialog appears, recovery is debounced and a healthy boot is not restarted. |
| Audio/display | Correct HDMI/USB sink, no dropouts, DPMS/blanking policy and touch-to-output mapping survive reboot. |
| Performance | Observe frame diagnostics and Pi thermals through at least two tracks, a rapid-skip sequence and 30 minutes of playback. No throttling or sustained long-frame growth. |
| Security | Remote guest gets 401 from owner routes; loopback kiosk works; malformed config is never overwritten; OAuth public origin/callback and Secure cookie are correct. |

Useful evidence commands:

```bash
watch -n 1 'vcgencmd measure_temp; vcgencmd get_throttled'
sudo dmesg --follow
sudo journalctl -f -u go-librespot -u spotify-display \
  -u spotify-wled -u spotify-network-watchdog
cat /run/spotify-display/wled-status.json | jq
sudo tcpdump -i any -n 'udp port 21324'
```

Keep the candidate under observation through at least one router/network
transition and one complete track transition before removing the old release.

## 7. Rollback

Rollback changes only the display stack; it must not reset NetworkManager or
delete the new account grant blindly.

```bash
systemctl --user stop spotify-kiosk spotify-pygame 2>/dev/null || true
sudo systemctl stop spotify-buttons spotify-wled spotify-wled.path \
  spotify-network-watchdog spotify-display go-librespot spotify-kiosk.service \
  raspotify
sudo systemctl disable spotify-buttons spotify-wled spotify-wled.path \
  spotify-network-watchdog spotify-display go-librespot \
  spotify-kiosk.service 2>/dev/null || true

for unit in go-librespot spotify-display spotify-network-watchdog \
  spotify-wled spotify-buttons spotify-kiosk; do
  if [ -e "$BACKUP_DIR/system/$unit.service" ]; then
    sudo cp -a "$BACKUP_DIR/system/$unit.service" /etc/systemd/system/
  else
    sudo rm -f "/etc/systemd/system/$unit.service"
  fi
done
if [ -e "$BACKUP_DIR/system/spotify-wled.path" ]; then
  sudo cp -a "$BACKUP_DIR/system/spotify-wled.path" /etc/systemd/system/
else
  sudo rm -f /etc/systemd/system/spotify-wled.path
fi
for unit in spotify-kiosk spotify-pygame; do
  if [ -e "$BACKUP_DIR/user/$unit.service" ]; then
    cp -a "$BACKUP_DIR/user/$unit.service" "$HOME/.config/systemd/user/"
  else
    rm -f "$HOME/.config/systemd/user/$unit.service"
    rm -f "$HOME/.config/systemd/user/graphical-session.target.wants/$unit.service"
  fi
done
[ ! -x "$BACKUP_DIR/bin/go-librespot" ] || \
  sudo install -m 0755 "$BACKUP_DIR/bin/go-librespot" /usr/local/bin/go-librespot

for item in \
  'raspotify.conf:/etc/raspotify/conf' \
  'spotify-display.conf:/etc/tmpfiles.d/spotify-display.conf' \
  '70-spotify-display-backlight.rules:/etc/udev/rules.d/70-spotify-display-backlight.rules'; do
  backup_name="${item%%:*}"
  destination="${item#*:}"
  if [ -e "$BACKUP_DIR/host/$backup_name" ]; then
    sudo install -D -m 0644 "$BACKUP_DIR/host/$backup_name" "$destination"
  else
    sudo rm -f "$destination"
  fi
done
for unit in spotify-kiosk spotify-pygame; do
  rm -f "$HOME/.config/systemd/user/graphical-session.target.wants/$unit.service"
  [ ! -e "$BACKUP_DIR/user/graphical-session.target.wants/$unit.service" ] || \
    cp -a "$BACKUP_DIR/user/graphical-session.target.wants/$unit.service" \
      "$HOME/.config/systemd/user/graphical-session.target.wants/"
done

sudo systemctl daemon-reload
systemctl --user daemon-reload
sudo systemd-tmpfiles --create /etc/tmpfiles.d/spotify-display.conf \
  2>/dev/null || true
sudo udevadm control --reload-rules 2>/dev/null || true

tail -n +2 "$BACKUP_DIR/unit-state.tsv" | \
while IFS=$'\t' read -r scope unit active enabled; do
  if [ "$scope" = user ]; then
    ctl=(systemctl --user)
  else
    ctl=(sudo systemctl)
  fi
  case "$enabled" in
    enabled|enabled-runtime|linked|linked-runtime) "${ctl[@]}" enable "$unit" ;;
    disabled) "${ctl[@]}" disable "$unit" 2>/dev/null || true ;;
    masked|masked-runtime) "${ctl[@]}" mask "$unit" ;;
  esac
  case "$active" in
    active|activating|reloading) "${ctl[@]}" start "$unit" ;;
    inactive|failed|deactivating) "${ctl[@]}" stop "$unit" 2>/dev/null || true ;;
  esac
done
curl -fsS http://127.0.0.1:5000/api/health | jq
```

If the old release path is selected through a symlink, repoint that symlink
atomically instead of copying code. Restore user service files from the backup
when they differ, and restore any optional enablement shown in
`$BACKUP_DIR/unit-files.txt`. The versioned procedure leaves the old release's
`config.json` untouched, so rollback does not copy an old token over a new one.
Reboot only after the known-good services have been restored and verified.

Files under `/run/spotify-display` are ephemeral and do not need restoration.
Use the backed-up config only for disaster recovery. Do **not** overwrite a
newly rotated refresh token with an older backup without first
disconnecting/revoking the current Spotify grant; that can create a confusing
partial-account rollback. Group membership is intentionally retained. Do not
blindly reverse NetworkManager or compositor/blanking state; compare the
recorded host files and restore them only if the release changed them and the
display or reconnection behaviour regressed.

## Release record

After acceptance, append the date, git commit, config schema decisions, unit
verification output, hardware results, max temperature/throttle status and any
deviations to `docs/REMEDIATION.md` or the appliance change log. Until then the
correct status is “repository complete; live Pi acceptance pending.”
