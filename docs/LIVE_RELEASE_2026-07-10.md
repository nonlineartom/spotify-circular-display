# Live Pi release record — 2026-07-10

## Outcome

The audited remediation is live on `pi5.local` (`192.168.68.74`) and survived a
full reboot. The system manager reports `running`, no system or user unit is
failed, and the production API is served by Waitress with the
`Spotify-Pi-Display` server identity.

| Item | Released value |
|---|---|
| GitHub branch | `codex/multiphase-remediation` |
| Accepted source commit | `a575359c5c45b6712b28a3c56f1dd3d5971e771f` |
| Pi release directory | `/home/admin/spotify-circular-display-remediation-dd6a4d7` |
| Preserved old installation | `/home/admin/circle-pi-display` |
| Backup/evidence directory | `/home/admin/spotify-display-backups/remediation-20260710-212609` |
| Final backup manifest SHA-256 | `d6776e1f8d2c2c2ce08acfb7d0e3abd82925df7dfb9e8ac65dfc33b3366cf0c9` |
| Reboot boot ID | `bfe3cfa3-8e8a-45ee-9fdb-e433705fab20` |

The release directory name was kept stable after the virtual-environment path
finding; the full Git SHA, not the directory suffix, is the source of truth.

## SSH and Git reconciliation

The original SSH blocker was identity selection, not network reachability or a
bad host key. The Pi accepts the dedicated identity explicitly:

```bash
ssh -i ~/.ssh/id_ed25519_circle_pi -o IdentitiesOnly=yes admin@pi5.local
```

The ssh-agent had no identities and no Pi host stanza selected this nonstandard
key automatically. Passwordless `sudo -n` is available on the target.

No separate LCD-dimming commit existed on this GitHub origin. The temporary
`codex/waveshare-backlight` worktree remained uncommitted; its `backlight.py`
was byte-identical to the remediation implementation and all functional UI,
service, setup and test changes were already integrated here. That worktree was
left untouched.

## Validation evidence

- Host release gate: **156 tests passed**, plus Python compilation, all project
  shell syntax, inline JavaScript syntax, service rendering and
  `git diff --check`.
- Pi release gate at the pinned commit: **156 tests passed**.
- Installed system and user units passed the target's `systemd-analyze verify`.
- Runtime config is valid, writable and mode `0600` (`admin:spotify-display`).
- go-librespot receiver state is mode `0600` and survived both migration and
  reboot.
- The post-reboot checkout had zero tracked changes.

## Target-only findings fixed during rollout

| Finding | Resolution |
|---|---|
| Debian 13 rejected literal quotes around single-path `WorkingDirectory` and `PathChanged` values | Added systemd-safe single-path escaping and a target regression test (`4334e33`). |
| Raspberry Pi OS did not activate `graphical-session.target` | Moved kiosk/Pygame enablement to the active user `default.target` (`dd6a4d7`). |
| go-librespot 0.7.1 persisted the authenticated session in `state.json`, not `credentials.json` | Backed up and migrated both state formats; 0.7.4 loaded and authenticated from the preserved state (`5c2d137`). |
| Python console launchers retained an absolute shebang after the candidate directory was renamed | Setup now clears and recreates the locked venv, preventing stale paths and packages (`5c2d137`). |
| The live WLED used the supported legacy `wled.host` shape | Setup and cutover activation now recognize both legacy host and modern device-list forms (`a575359`). |

## Cutover chronology and rollback proof

The old checkout was never pulled, reset or overwritten. A 37-file protected
backup contains application/receiver state, the receiver binary, system/user
units and wants links, Raspotify configuration, tmpfiles/udev policy, host
baselines, journals, a post-reboot screenshot and acceptance output.

Three guarded attempts deliberately rolled back before the accepted cutover:

1. The old watchdog raced a simultaneous receiver stop and restarted it. The
   rollback restored the old stack; cutover ordering was changed to stop and
   verify the watchdog first.
2. A renamed candidate exposed the stale Waitress shebang. Receiver startup
   succeeded, the display health gate failed, and rollback restored the old
   stack. The installer-level `venv --clear` fix followed.
3. The complete candidate stack became healthy, but the deployment script
   treated `systemctl is-enabled`'s expected nonzero status for disabled
   Raspotify as an error. The safety trap rolled back again; the assertion was
   corrected without changing the application.

The fourth cutover passed receiver, Waitress, server-identity, watchdog, WLED
path and user-kiosk gates. These rollbacks are operational evidence that the
backup and restoration path works, not hidden failed releases.

## Live and post-reboot acceptance

| Area | Result |
|---|---|
| Core services | go-librespot, display, watchdog, WLED service/path and user kiosk active/enabled; system kiosk, Raspotify and GPIO inactive as intended. |
| HTTP | `/api/health` healthy; `/api/info` advertises `192.168.68.74:5000`; server header is `Spotify-Pi-Display`. |
| Receiver | go-librespot 0.7.4 loaded persisted credentials and authenticated AP/Login5 before and after reboot. |
| Kiosk boot | Chromium returned automatically from user `default.target` on Wayland at 1080×1080. |
| Backlight | Exact `0712:000a`/`hidraw0` device available through group `spotify-backlight`; controlled idle settled at logical 10%/physical 8%, active settled at logical 100%/physical 80%, with no USB reset or undervoltage warning. |
| WLED | Legacy 46-pixel device normalized successfully; renderer/path active, runtime schema v1 healthy, zero local UDP errors while idle. |
| Security boundary | Remote diagnostics/crate/WLED owner routes returned 401; remote HID mutation returned 403; loopback owner access returned 200 only with a literal loopback Host, while a rebound Host returned 401. |
| Visual | Pre- and post-reboot 1080×1080 captures showed correct circular mask, crate geometry, artwork depth/reflection, typography and edge treatment. The retained post-reboot PNG SHA-256 is `de7b707b479cfc37aeb1dabf14d07e6e155d63948aa88da7762b7a316f6357a6`. |
| Idle efficiency | Four-second capture sampling showed only the intended low-amplitude GPU ambient drift; measured aggregate Chromium CPU was 0.00% of one core during the sample. |
| Thermal/power | Maximum observed temperature was 55.4°C; post-reboot temperature was 52.7°C; `vcgencmd get_throttled` remained `0x0`. |
| Logs | No priority-warning entries for receiver/display/WLED/watchdog and no relevant kernel HID/USB/undervoltage errors after reboot. |

## Hands-on checks still requiring an operator

The live software, reboot and hardware-control gates are complete. The
following checks require deliberate playback, physical touch/audio observation
or a real router outage and were not fabricated remotely:

- play/pause/seek/skip and audio-output listening with a real track;
- touch gesture feel, first-touch wake drain and long-session animation quality;
- WLED pixels during an actual playing→paused→idle transition;
- GPIO buttons, which remain intentionally disabled until wiring is confirmed;
- a router/AP outage and recovery, plus the 30-minute playback/thermal soak.

The backup and old release must be retained until those checks have passed in
normal use.
