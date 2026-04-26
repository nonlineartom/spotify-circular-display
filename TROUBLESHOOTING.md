# Troubleshooting and QoL notes

## Wi-Fi popup appears over the kiosk

This popup is not Chromium. It is the desktop network agent asking for attention
after Wi-Fi drops, so Chromium kiosk flags cannot dismiss it.

Best fix: run the display as an appliance instead of a normal Ubuntu desktop:

- Use Raspberry Pi OS Lite / Ubuntu Server with a minimal display stack.
- Configure Wi-Fi as a system connection, not a per-user desktop/keyring secret.
- Avoid starting `nm-applet`, GNOME Settings, or other desktop notification agents
  in the kiosk session.

Useful NetworkManager hardening commands:

```bash
nmcli connection show
sudo nmcli connection modify "YOUR_WIFI_NAME" connection.autoconnect yes
sudo nmcli connection modify "YOUR_WIFI_NAME" connection.autoconnect-retries 0
sudo nmcli connection modify "YOUR_WIFI_NAME" 802-11-wireless.powersave 2
sudo nmcli connection modify "YOUR_WIFI_NAME" 802-11-wireless-security.psk-flags 0
sudo systemctl restart NetworkManager
```

If you stay on Ubuntu Desktop, also disable network notifications for the kiosk
user where the desktop supports it:

```bash
gsettings set org.gnome.nm-applet disable-connected-notifications true
gsettings set org.gnome.nm-applet disable-disconnected-notifications true
```

## Spotify Connect disappears until reboot

This is usually Raspotify/librespot getting stuck after a network transition. The
included `spotify-network-watchdog` service restarts `raspotify`,
`spotify-display`, and the kiosk when the default route comes back. That is much
lighter than rebooting the Pi.

Manual recovery:

```bash
sudo systemctl restart raspotify spotify-display spotify-kiosk
```

Useful logs:

```bash
sudo journalctl -u raspotify -f
sudo journalctl -u spotify-network-watchdog -f
sudo journalctl -u spotify-display -f
```

## Record keeps spinning after playback stops

The display is driven by `/tmp/spotify-state.json`, which is written by
Raspotify's `--onevent` hook. If a stop/end event is missed, the old state can
look like it is still playing. The server now stops trusting a "playing" event
after the expected track end plus a small grace period.

Check the raw event state:

```bash
curl http://localhost:5000/api/health
cat /tmp/spotify-state.json
```

## Good QoL upgrades

- Add a small physical restart button that runs
  `sudo systemctl restart raspotify spotify-display spotify-kiosk`.
- Add Ethernet if the display is fixed in one place. Spotify Connect discovery
  is much calmer on wired network.
- Add a local admin page with service status, Wi-Fi SSID, IP address, and buttons
  to restart Raspotify/kiosk.
- Add a boot splash or "network reconnecting" state so Wi-Fi loss looks
  intentional instead of frozen.
- Move secrets out of `config.json` into an environment file readable only by
  the service user.
