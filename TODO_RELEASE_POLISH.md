# Release polish TODO

Last reviewed: 2026-07-19

## Current state

- [x] Multi-user household profile implementation completed.
- [x] Unlinked listeners receive House picks without private library data.
- [x] Explicit temporary guest profiles remain time-bounded.
- [x] Six-month reauthorization lifecycle implemented for newly authorized profiles.
- [x] LAN-only HTTPS nginx configuration, installer and verifier authored.
- [x] Host validation passed: 215 tests.
- [x] Raspberry Pi validation passed: 215 tests.
- [x] Commit `d8c1f5e` pushed to `codex/multiphase-remediation` and deployed.
- [x] Live checkout is clean at `/home/admin/spotify-circular-display-auth-touch-413dfff`.
- [x] go-librespot, display, kiosk, WLED and network watchdog are healthy with zero restarts.
- [x] Extended playback remained stable with no throttling or WLED send failures.
- [x] 2026-07-19: commit `0fca6af` deployed live (swipe direction, DNS-probing
      watchdog with stuck-receiver recovery, indefinite household idle shelf);
      host suite 220 tests. See `docs/LIVE_RELEASE_2026-07-10.md` § 2026-07-19.
- [ ] Current commit `0fca6af` has not yet been reboot-tested.
- [ ] Upgrade the go-librespot binary: nightly `panic: send on closed channel`
      (`daemon/app.go:375`) drops active casts (in progress in a separate session).
- [ ] Investigate the unclean reboots recorded 07-10/07-14/07-17 (suspected
      hardware-watchdog resets under memory pressure on the 1 GB board).
- [ ] Phone pairing is not live: the Pi still uses the HTTP loopback OAuth origin and has no nginx/443 listener.

Keep the unrelated `ESP32P4_PORT_FEASIBILITY.md` file untouched unless separately requested.

## 1. Fix the release gate

- [ ] Fix GitHub Actions on Python 3.13.
  - Python 3.11 currently passes.
  - Python 3.13 fails while building `lgpio` because the runner cannot link `-llgpio`.
  - Either install the correct native library on the runner or separate Pi-only hardware dependencies from the portable CI environment.
- [ ] Run the complete workflow successfully on Python 3.11 and 3.13.
- [ ] Confirm local `bash scripts/validate.sh` still passes.
- [ ] Confirm `git diff --check` is clean.

Failed workflow at the time of this handoff:
https://github.com/nonlineartom/spotify-circular-display/actions/runs/29160297967

## 2. Activate LAN-only HTTPS phone pairing

External prerequisites:

- [ ] Reserve `192.168.68.74` for the Pi in DHCP, or choose another fixed RFC1918 address.
- [ ] Choose a hostname under a domain controlled by the owner.
- [ ] Choose and test a DNS model:
  - controlled split DNS; or
  - a public A record resolving to the Pi's private address.
- [ ] Confirm intended phones accept the DNS answer and do not block it as rebinding.
- [ ] Do not publish an AAAA record unless IPv6 is separately designed and reviewed.
- [ ] Obtain a publicly trusted certificate using DNS-01.
- [ ] Provision the full chain and mode-0600 private key onto the Pi.
- [ ] Register exactly `https://<hostname>/callback` in the Spotify developer dashboard.
- [ ] Add every intended household Spotify account to the app's allowed users.
- [ ] Confirm the router has no port-forward, DMZ-host rule or UPnP mapping for ports 80/443.

Activation:

- [ ] Fill `deploy/lan-https.json` from `deploy/lan-https.example.json`.
- [ ] Set `public_base_url` to `https://<hostname>` in the live `config.json`.
- [ ] Set `redirect_uri` to `https://<hostname>/callback`.
- [ ] Run the dry-run certificate/configuration validation first.
- [ ] Review the rendered nginx site.
- [ ] Run `scripts/install-lan-https.sh --activate` only after every gate passes.
- [ ] Run `scripts/verify-lan-https.sh` on the Pi.
- [ ] Confirm nginx listens only on the selected LAN address at port 443.
- [ ] Confirm Waitress listens only on `127.0.0.1:5000` after activation.
- [ ] Confirm all `/api` routes return 404 through nginx.
- [ ] Test from an intended phone on Wi-Fi using normal certificate validation.
- [ ] Test with Wi-Fi disabled and confirm the hostname is unreachable over mobile data.

Ongoing operation:

- [ ] Automate off-device DNS-01 renewal.
- [ ] Deploy renewed certificate files atomically.
- [ ] Run `nginx -t` before every reload.
- [ ] Add certificate-expiry monitoring and failure notification.
- [ ] Exercise certificate deployment and nginx rollback in a controlled test.

Full procedure: `docs/DEPLOYMENT.md`, section "LAN-only HTTPS pairing ingress".

## 3. Real multi-user acceptance

- [ ] Reauthorize the existing migrated household profile after HTTPS activation.
  - Its legacy authorization timestamp is unknown.
  - Relinking starts the defined six-month reauthorization deadline.
- [ ] Link household account A while A controls Pi Display.
- [ ] Link household account B while B controls Pi Display.
- [ ] Confirm A sees only A's playlists, albums and rotation.
- [ ] Confirm B sees only B's playlists, albums and rotation.
- [ ] Test A to B to A handoffs.
- [ ] Test rapid A to B to A handoffs while crate requests are in flight.
- [ ] Confirm stale shelves clear immediately on every handoff.
- [ ] Confirm stale launches and old pairing links are rejected.
- [ ] Restart display and go-librespot and confirm both mappings survive.
- [ ] Confirm an unlinked third account sees House picks only.
- [ ] Test explicit bounded guest pairing and expiry.
- [ ] Move one profile into the reauthorization-due state and verify the safe House-picks fallback and relink flow.

## 4. Physical appliance acceptance

- [ ] Reboot the Pi on `d8c1f5e` and confirm the kiosk returns automatically.
- [ ] Confirm no failed or crash-looping system/user units after reboot.
- [ ] Confirm the touchscreen retains calibration matrix `-1 0 1 0 -1 1`.
- [ ] Test physical left and right edge taps.
- [ ] Test physical left and right single-finger swipes.
- [ ] Test vertical directions and first-touch wake drain.
- [ ] Test two-finger play/pause.
- [ ] Test two-finger seek twist.
- [ ] Test two-finger volume drag.
- [ ] Test pinch-in crate and pinch-out tracklist.
- [ ] Test three-finger hardware-brightness drag.
- [ ] Confirm pointer cancellation or accidental capture changes trigger no action.
- [ ] Listen for correct play, pause, seek, skip, previous and volume behavior.
- [ ] Observe WLED during playing, paused, track transition and idle states.
- [ ] Confirm the correct audio sink and no dropouts.
- [ ] Temporarily interrupt the AP/route and verify unattended recovery.
- [ ] Observe at least two complete tracks and one rapid-skip sequence.
- [ ] Record a minimum 30-minute physical audio/visual/thermal soak.
- [ ] Record maximum temperature and `vcgencmd get_throttled` output.
- [ ] Leave GPIO buttons disabled unless their wiring is actually installed and verified.

## 5. Small implementation and host polish

- [ ] Replace unsupported udev `ENV{LIBINPUT_CALIBRATION_MATRIX}:=` syntax with `=`.
- [ ] Re-run the touch calibration and reboot checks after changing the udev rule.
- [ ] Correct the netplan file-permission warning on the Pi.
- [ ] Investigate repeated 3.5-second lyrics lookup timeouts.
- [ ] Add bounded backoff/rate-limited logging for lyrics provider timeouts.
- [ ] Clarify or rename the `raspotify_state: state_file_missing` health diagnostic when go-librespot is healthy.
- [ ] Distinguish Spotify app-allowlist failures, rate limiting and provider outages in OAuth diagnostics.

## 6. Browser and deployment test polish

- [ ] Add real Chromium/Playwright tests for pairing, handoff and crate privacy.
- [ ] Add browser-level pointer tests for the complete gesture lifecycle.
- [ ] Add executable TLS activation and rollback tests in a disposable environment.
- [ ] Add ShellCheck and suitable Python lint/security checks to validation.
- [ ] Decide whether to add type checking and a practical coverage threshold.
- [ ] Pin GitHub Actions dependencies to reviewed immutable commit SHAs.

## 7. Documentation and release hygiene

- [ ] Update `CHANGELOG.md` from the old default-guest wording to persistent household profiles plus explicit bounded guests.
- [ ] Add a `d8c1f5e`/215-test entry to `docs/LIVE_RELEASE_2026-07-10.md`.
- [ ] Reconcile stale test counts and live-Pi statements in `docs/REMEDIATION.md` and `README.md`.
- [ ] Record the final HTTPS hostname model without publishing credentials.
- [ ] Record certificate renewal ownership and recovery steps.
- [ ] Record real multi-account, reboot, physical and soak results.
- [ ] Get GitHub Actions green.
- [ ] Open a pull request from `codex/multiphase-remediation` to `main`.
- [ ] Review and merge the five outstanding branch commits.
- [ ] Tag or otherwise identify the accepted release.
- [ ] Decide on a project license if the repository is intended for public reuse.
- [ ] Retain one known-good rollback release and protected backup.
- [ ] Remove older redundant release directories/backups only after final acceptance.

## Optional improvements that should not block release

- [ ] Show a QR code for the one-use household pairing URL.
- [ ] Add a local owner portal for profile status, disconnect and bounded-guest creation.
- [ ] Split the large `server.py` into auth/profile, playback, crate and hardware modules.
- [ ] Split the inline kiosk template into versioned JavaScript and CSS modules.
- [ ] Refine WLED discovery with mDNS/D-Bus only if it remains operationally simpler.
- [ ] Add ambient-light or night scheduling only after defining the desired hardware and policy.

## Definition of done

- [ ] GitHub Actions and host/Pi validation are green.
- [ ] Trusted LAN HTTPS pairing works without a tunnel or WAN exposure.
- [ ] Two real household accounts pass handoff/privacy acceptance.
- [ ] An unlinked account receives House picks only.
- [ ] Current release survives reboot and the complete physical acceptance matrix.
- [ ] Certificate renewal is automated and monitored.
- [ ] Documentation matches the deployed release.
- [ ] The accepted work is merged to `main` with a retained rollback path.
