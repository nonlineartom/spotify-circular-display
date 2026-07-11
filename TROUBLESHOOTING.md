# Troubleshooting and QoL notes

## Display brightness / hardware backlight

The Waveshare **7inch 1080×1080 LCD (HDMI Round)**—EDID model `WS070Round`,
USB touch controller `0712:000a` ("Waveshare-079-HD")—does support Linux
backlight control. It is not exposed through `/sys/class/backlight` or DDC/CI;
it uses a vendor output report on the same Touch USB HID device.

The Linux hidraw write is five bytes, including the report ID:

```text
09 08 F7 LL CC
```

- `09` is HID report ID 9.
- `LL` is the physical brightness byte: `round(percent × 2.5)`, normally
  `0x00`–`0xFA` for 0–100%.
- `CC` is `LL XOR 0xFF`.

The application discovers the current `/dev/hidraw*` node by USB VID/PID,
writes only that report, and rediscovers after `ENODEV`/USB re-enumeration. Do
not send the demo controller's `5A A5 FF 00` boot/test packets, and do not flash
firmware intended for a different Waveshare display.

`setup.sh` installs the exact-device rule
`/etc/udev/rules.d/70-spotify-display-backlight.rules`, which grants mode `0660`
to group `spotify-backlight`; the service receives that as a supplementary
group. There is intentionally no `chmod 777` fallback. The rule is retriggered
during setup and automatically reapplied on every reconnect.

On the current Pi 5's 3 A supply, the controller has previously re-enumerated
during a large brightness jump. The shipped policy therefore maps logical
0–100 brightness onto a maximum of 80% physical output. Public choices remain
10-point values, but the HID worker interpolates transitions in 1-point steps
every 15 ms by default. The first command on startup/reconnect is still capped
at logical 10%; requests made during a ramp are coalesced onto the latest target.
The controller also polls the resolved HID contact identity, so a reset from
an instance such as `.0002` to `.0003` is noticed even when the kernel reuses
the same `hidraw0` basename. Idle uses at most logical 10%, and wake restores
the remembered active level through the same ramp. Do not enable
`usb_max_current_enable=1` or raise
`safe_max_percent` while retaining this supply. This is a conservative operating
limit, not a substitute for adequate power: if `dmesg` reports USB over-current
or touch drops out, use the panel's dedicated 5 V Power USB-C input or a
correctly detected Pi 5 5 A supply.

Three-finger vertical drag controls brightness and shows the radial bar on the
left rim; two-finger vertical drag remains volume on the right. The idle dimmer
now lowers the hardware backlight as well as fading the UI.

If brightness is unavailable, check detection and permissions:

```bash
lsusb -d 0712:000a
getent group spotify-backlight
stat -c '%A %U %G %n' /dev/hidraw*
sudo udevadm control --reload-rules
sudo udevadm trigger --action=change --subsystem-match=hidraw
sudo systemctl restart spotify-display
curl -s http://127.0.0.1:5000/api/backlight | jq
sudo journalctl -u spotify-display -n 100 --no-pager
```

An absent `/sys/class/backlight` remains expected for this HDMI panel; it does
not indicate that vendor HID control is unavailable.

## Touch targets and gestures are inverted

If touching the left activates the right side and dragging up moves down, both
absolute axes need the panel's 180-degree calibration. Do not compensate in the
web page: Chromium performs native hit-testing before application gesture code,
so a JavaScript-only inversion would leave buttons and links wrong.

`setup.sh` defaults `TOUCH_ROTATION=180` for the Waveshare `0712:000a`
controller and installs
`/etc/udev/rules.d/70-spotify-display-touch.rules`. The rule sets libinput's
absolute-coordinate matrix only for that exact touchscreen. For a differently
mounted panel, rerun setup with one of the supported orientations:

```bash
TOUCH_ROTATION=0 ./setup.sh       # no coordinate rotation
TOUCH_ROTATION=90 ./setup.sh      # 90 degrees clockwise
TOUCH_ROTATION=180 ./setup.sh     # normal round-panel mounting (default)
TOUCH_ROTATION=270 ./setup.sh     # 270 degrees clockwise
```

Reconnect Touch USB or reboot after changing the rule. Confirm the applied
matrix with:

```bash
sudo libinput list-devices | sed -n '/Waveshare/,/^$/p'
```

For the default orientation, `Calibration` should no longer report the identity
matrix; the effective six values are `-1 0 1 0 -1 1`.

## Multi-touch gestures aren't recognized (single taps/swipes work)

If multi-finger gestures (twist-seek, pinch, volume, three-finger brightness or
two-finger tap) do nothing while single-finger taps and swipes work fine, the
compositor is almost certainly converting touch into emulated mouse input—one
pointer only, with the additional fingers silently dropped.

On labwc (Raspberry Pi OS Wayland) check `~/.config/labwc/rc.xml` and
`/etc/xdg/labwc/rc.xml` for your touch device:

```xml
<touch deviceName="Waveshare  Waveshare -079-HD" mapToOutput="HDMI-A-1" mouseEmulation="yes"/>
```

`mouseEmulation="yes"` (which Waveshare's own setup instructions use) is the
culprit. Set it to `"no"` — keep the `mapToOutput`, it pins the touch
coordinates to the right display:

```xml
<touch deviceName="Waveshare  Waveshare -079-HD" mapToOutput="HDMI-A-1" mouseEmulation="no"/>
```

Then reload labwc and re-add the panel (or just reboot):

```bash
kill -HUP $(pgrep -x labwc)
# soft-replug the USB touch panel so it re-attaches under the new config
echo 0 | sudo tee /sys/bus/usb/devices/<usb-port>/authorized
echo 1 | sudo tee /sys/bus/usb/devices/<usb-port>/authorized
```

Chromium synthesizes clicks from native touch itself, so single-finger
controls keep working — and the page receives real `pointerType: "touch"`
events for every finger.

Diagnosis tip: the panel itself can be checked with
`grep -A5 Waveshare /proc/bus/input/devices` (look for `hid-multitouch` in
dmesg — this one reports 10 contacts). If the kernel side is fine but the
page sees `pointerType: "mouse"`, it's the compositor's emulation.

## Wi-Fi password popup appears over the kiosk (e.g. after a nightly router reboot)

**Symptom:** in the morning the display shows a Linux Wi-Fi password prompt with
the field already full of dots, and only a power cycle clears it.

That popup is **not** Chromium — `--kiosk` flags cannot touch it. It is the desktop
NetworkManager **secret agent** (on Bookworm/labwc that is `wf-panel-pi`'s network
plugin; on older images, `nm-applet`) drawing a GTK "password for the wireless
network" dialog. The "characters already filled in" are the saved PSK echoed back
as masked dots — not something you typed.

**Why it happens:** the Wi-Fi profile created by Raspberry Pi Imager / the desktop
wizard stores the PSK as an *agent-owned* secret (`psk-flags = 1`). When the router
reboots overnight and the AP is gone for a few minutes, NetworkManager burns through
its association-retry budget, can't tell a missing AP from a wrong key, transitions
to `NEED_AUTH`, and asks the session agent for a "new" secret (`GetSecrets` with the
`REQUEST_NEW` flag). The agent pops the dialog over the kiosk and waits forever for a
human. Restarting the kiosk only relaunches Chromium — a different process — so the
dialog stays put, which is why only a power cycle has been clearing it.

**This is now fixed automatically**, in two layers. `setup.sh` runs
`harden-network.sh`, which, for the active Wi-Fi profile:

- makes the PSK **system-owned** (`802-11-wireless-security.psk-flags 0`) so
  NetworkManager answers its *normal* secret requests from its own store and does
  not consult a desktop agent;
- sets `connection.autoconnect yes`, `connection.autoconnect-retries 0` (retry
  forever) and `802-11-wireless.powersave 2` (off), so a multi-minute outage never
  exhausts the retry budget and the radio re-associates quickly when the AP returns;
- masks/kills any standalone desktop secret agent in the kiosk session.

That covers the common case, but it is **not sufficient on its own**: when the AP
vanishes *mid-association* (exactly what a router reboot does), NM can mis-read it
as a wrong key and ask an agent for a *new* secret (`GetSecrets` with `REQUEST_NEW`)
**regardless of `psk-flags`** — drawing the prompt — and when no agent answers, the
activation fails with `no-secrets`, which **blocks autoconnect**. That block is not
cleared by `autoconnect-retries`; only an explicit `nmcli connection up` clears it.

So the load-bearing layer is the **`spotify-network-watchdog`**: while the network
is down it keeps re-issuing `nmcli connection up` for the Wi-Fi profile. That uses
the stored PSK (no prompt), reconnects within seconds of the AP returning, clears
the `no-secrets` autoconnect block, and cancels NM's outstanding secret request —
which dismisses any dialog an agent managed to draw. The Pi self-heals after a
nightly router reboot with no power cycle.

Re-run it any time — it is idempotent and auto-detects the active Wi-Fi (no SSID to
edit):

```bash
./harden-network.sh
# If the saved key can't be read back from the keyring (e.g. over SSH), supply it
# once — the connection is left untouched until you do, so Wi-Fi never breaks:
WIFI_PSK='your-wifi-password' ./harden-network.sh
```

Verify it took (must print `0`):

```bash
CONN=$(nmcli -t -f NAME,TYPE,DEVICE connection show --active | awk -F: '$2 ~ /wireless/ {print $1; exit}')
nmcli -g 802-11-wireless-security.psk-flags connection show "$CONN"
```

Manual equivalent (the old commands, for reference):

```bash
sudo nmcli connection modify "$CONN" 802-11-wireless-security.psk-flags 0
sudo nmcli connection modify "$CONN" connection.autoconnect yes
sudo nmcli connection modify "$CONN" connection.autoconnect-retries 0
sudo nmcli connection modify "$CONN" 802-11-wireless.powersave 2
sudo systemctl restart NetworkManager
```

> **Caution:** re-editing Wi-Fi through the desktop wizard can reset `psk-flags`
> back to `1` (agent-owned) and reintroduce the prompt. If it ever comes back, just
> re-run `./harden-network.sh`. The bulletproof long-term posture is a true
> appliance image — Pi OS Lite + a bare compositor (e.g. `cage`), with no desktop
> panel or secret agent at all.

## Spotify Connect disappears until reboot

This is usually the Spotify Connect receiver failing to re-advertise after a
confirmed network transition. The remediated `spotify-network-watchdog`
debounces route state, reactivates the selected Wi-Fi device while offline and
restarts **only the receiver** after the route has recovered. It performs no
boot-time restart and does not kill/restart the healthy Flask or graphical
session. A legacy Raspotify fallback is attempted only when installed and the
go-librespot health check actually fails.

Manual recovery:

```bash
sudo systemctl restart go-librespot
curl -fsS http://127.0.0.1:3678/status | jq

# Only if the API itself is unhealthy:
sudo systemctl restart spotify-display
curl -fsS http://127.0.0.1:5000/api/health | jq

# Kiosk is a graphical user unit, not a system unit:
systemctl --user restart spotify-kiosk
```

Useful logs:

```bash
sudo journalctl -u go-librespot -f
sudo journalctl -u spotify-network-watchdog -f
sudo journalctl -u spotify-display -f
```

The watchdog's first journal line should say that the initial state was
observed without a boot-time restart. Repeated recovery with no real route
transition indicates a bad route target, driver issue or NetworkManager profile,
not a reason to shorten the debounce.

## Record keeps spinning after playback stops

The display prefers go-librespot's live local API. An explicitly enabled older
Raspotify installation writes schema-bounded fallback state through its
`--onevent` hook. Current runtime state is under `/run/spotify-display`; the old
`/tmp/spotify-state.json` is read only for migration compatibility. If a
stop/end event is missed, the server stops trusting a "playing" event after the
expected track end plus a small grace period.

Check the raw event state:

```bash
curl http://localhost:5000/api/health
curl http://localhost:3678/status
cat /run/spotify-display/spotify-state.json | jq
sudo journalctl -u go-librespot -u spotify-display -n 100 --no-pager
```

An HTTP/receiver error is not treated as a real idle event in the browser or
WLED renderer. The UI retains the last valid track with an offline indication;
WLED retains its last active snapshot for eight seconds before releasing. A
confirmed network-down transition published by the watchdog is deliberately a
real idle state.

## WLED bar animation looks choppy or stepped

The gradient drift is rendered server-side and streamed to WLED at
`wled.play_fps` (default 30, bounded to 60). If you see discrete jumps, either
raise `play_fps` gradually or lengthen `gradient_drift_seconds`; very long strips
cannot move by less than one LED per frame at the default drift period. The
renderer reuses frames for identical strip shapes, but each unique device shape
still costs work and network bandwidth.

Check the actual packet cadence on the Pi:

```bash
sudo tcpdump -i any -n 'udp port 21324' -c 60
```

Inter-packet gaps should be roughly `1000 / play_fps` milliseconds.

Also inspect renderer health and logs:

```bash
cat /run/spotify-display/wled-status.json | jq
sudo journalctl -u spotify-wled -n 150 --no-pager
```

If pause is choppy, do not raise `pause_fps` first. The four-second motor ramp
should retain the playing cadence while settling and switch to the paused rate
only afterwards. A premature cadence drop indicates the new service is not the
one running or the configuration failed to reload.

## Configuration is reported malformed or write-protected

The API deliberately refuses to replace an existing malformed, oversized,
wrong-shaped or unreadable `config.json`. This prevents a transient read error
or bad edit from destroying credentials.

```bash
curl -s http://127.0.0.1:5000/api/health | jq '.config'
stat -c '%a %U %G %n' config.json
cp -a config.json "config.json.bad.$(date +%s)"
jq empty config.json
```

Repair a copy at the console, validate it with `jq`, preserve mode `0600`, then
atomically move it into place and restart only `spotify-display`. Never use a
WLED or OAuth API call as a way to “repair” a corrupt file.

## Owner route returns 401

Loopback owner trust requires both a loopback peer and literal loopback Host.
This is why `curl http://127.0.0.1:5000/api/crate` works locally while the Pi's
LAN hostname does not automatically become owner-authorized. For remote owner
work, configure a long `security.owner_token` and use a bearer header or signed
owner session. Pairing additionally requires one canonical `public_base_url`
and the exact same-origin `/callback` Spotify redirect.

Do not weaken the loopback/Host check or blindly trust `X-Forwarded-Host` to fix
a reverse-proxy mistake. The proxy must preserve the public Host rather than
send its `127.0.0.1` backend Host, and should deny `/api/` while allowing the
five phone OAuth routes documented in `docs/SECURITY.md`. From a machine using
the public origin, verify `/api/auth/status` returns 401 or 404 without an owner
token; a 200 response means the proxy has accidentally inherited kiosk trust.

## The crate shows House picks instead of my library

House picks are the privacy-safe result whenever the active Spotify Connect
account has no verified Web API profile. Start playback from the intended
Spotify account, open the crate, then type the displayed one-use URL into that
listener's phone and authorize the same account that is controlling Pi Display.
Authorizing a different Spotify account is rejected.

If no URL appears, confirm that `public_base_url` and the exact same-origin
`/callback` are registered in the Spotify dashboard, then check redacted owner
status locally:

```bash
curl -s http://127.0.0.1:5000/api/auth/status | jq '{profile_state, legacy_grant_pending, profiles}'
```

Spotify Development Mode admits only a small authorized-user set, so intended
listeners must also be added in the app dashboard. An expired guest grant or a
Spotify `invalid_grant` intentionally returns that listener to House picks;
pair again rather than copying another account's token into `config.json`.

During a handoff, the old shelf should clear immediately. If it does not, do
not weaken the epoch checks: verify that go-librespot `/status` includes a
string `username`, that the display and server were upgraded together, and
inspect the service log without printing the username or any token.

## Candidate future upgrades

- Add a QR-based graphical owner portal for pairing/status/logout.
- Add an ambient-light sensor or local night schedule after defining the desired
  hardware and minimum wake brightness.
- Split the large kiosk template into versioned JS/CSS modules after capturing a
  production visual baseline.
- Prefer wired Ethernet for a fixed installation and place owner endpoints on a
  deliberately filtered appliance network.
- Export redacted health counters to a local monitor, without exporting tokens,
  private library content or Wi-Fi credentials.
