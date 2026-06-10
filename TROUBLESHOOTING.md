# Troubleshooting and QoL notes

## Multi-touch gestures aren't recognized (single taps/swipes work)

If two-finger gestures (twist-seek, pinch, two-finger tap) do nothing while
single-finger taps and swipes work fine, the compositor is almost certainly
converting touch into emulated mouse input — one pointer only, the second
finger silently dropped.

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

**This is now fixed automatically.** `setup.sh` runs `harden-network.sh`, which, for
the active Wi-Fi profile:

- makes the PSK **system-owned** (`802-11-wireless-security.psk-flags 0`) — the
  load-bearing fix: NetworkManager answers its own secret requests and never
  consults an agent, so the dialog can no longer be raised;
- sets `connection.autoconnect yes`, `connection.autoconnect-retries 0` (retry
  forever) and `802-11-wireless.powersave 2` (off), so a multi-minute outage never
  exhausts the retry budget and the radio re-associates quickly when the AP returns;
- masks/kills any standalone desktop secret agent in the kiosk session.

The `spotify-network-watchdog` now also dismisses a stuck dialog and re-activates
Wi-Fi when the network returns, so already-deployed units recover without a power
cycle.

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

This is usually the Spotify Connect receiver getting stuck after a network
transition. The included `spotify-network-watchdog` service restarts
`go-librespot` (or `raspotify` on older installs), `spotify-display`, and the
kiosk when the default route comes back. That is much lighter than rebooting the
Pi.

Manual recovery:

```bash
sudo systemctl restart go-librespot spotify-display spotify-kiosk
```

Useful logs:

```bash
sudo journalctl -u go-librespot -f
sudo journalctl -u spotify-network-watchdog -f
sudo journalctl -u spotify-display -f
```

## Record keeps spinning after playback stops

The display now prefers go-librespot's live local API. Older Raspotify installs
fall back to `/tmp/spotify-state.json`, which is written by Raspotify's
`--onevent` hook. If a stop/end event is missed, the server stops trusting a
"playing" event after the expected track end plus a small grace period.

Check the raw event state:

```bash
curl http://localhost:5000/api/health
curl http://localhost:3678/status
cat /tmp/spotify-state.json
```

## WLED bar animation looks choppy or stepped

The gradient drift is rendered server-side and streamed to WLED at `wled.play_fps`
(default 30). If you see discrete jumps instead of continuous motion, the strip
is moving more than ~1 LED per frame. Raise `play_fps` until it smooths out —
rule of thumb is `play_fps ≥ pixel_count / gradient_drift_seconds`. For a
100-LED strip with the default 1.8 s rotation period you want ≥ 56 FPS.

Check the actual packet cadence on the Pi:

```bash
sudo tcpdump -i any -n 'udp port 21324' -c 60
```

Inter-packet gaps should be roughly `1000 / play_fps` milliseconds.

## Good QoL upgrades

- Add a small physical restart button that runs
  `sudo systemctl restart go-librespot spotify-display spotify-kiosk`.
- Add Ethernet if the display is fixed in one place. Spotify Connect discovery
  is much calmer on wired network.
- Add a local admin page with service status, Wi-Fi SSID, IP address, and buttons
  to restart Raspotify/kiosk.
- Add a boot splash or "network reconnecting" state so Wi-Fi loss looks
  intentional instead of frozen.
- Move secrets out of `config.json` into an environment file readable only by
  the service user.
