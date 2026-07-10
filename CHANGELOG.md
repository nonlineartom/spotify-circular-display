# Changelog

All notable changes to Spotify Circular Display are documented here. The format
follows Keep a Changelog; this project does not yet publish semantic version
numbers, so audit remediation is recorded under `Unreleased`.

## Unreleased

### Security

- Added an explicit owner boundary for private library data, OAuth management,
  WLED administration and detailed diagnostics while retaining local Spotify
  Connect playback.
- Added canonical-origin, exact-callback, state-bound PKCE OAuth; one-use guest
  linkage; expiring grants; and generation-safe disconnect/cache invalidation.
- Added request validation, body/rate bounds, browser-origin checks and security
  response headers, including bounded public SSE clients and auth `no-store`.
- Replaced direct configuration writes with locked, permission-restricted atomic
  updates that refuse to overwrite malformed or unreadable configuration.
- Restricted HID backlight writes to requests whose peer and literal Host are
  loopback; raw HID fields are not exposed through HTTP.

### Fixed

- Prevented playback poll, artwork/palette, tracklist and pointer-cancellation
  races in the kiosk.
- Corrected stale/failed artwork fallback, signed CSS artwork URLs, empty crate
  account transitions and tracklist/modal background inert state.
- Corrected inactivity dimming and stopped high-rate rendering while dimmed,
  idle or settled paused.
- Corrected WLED pause/idle/error state handling and smoothed spin transitions.
- Corrected Pi 5 GPIO, runtime-state, watchdog, service identity and startup
  assumptions.
- Corrected systemd single-path rendering so `WorkingDirectory` and path-unit
  directives pass target parsing while paths containing spaces remain valid.
- Enabled graphical user services from `default.target` because Raspberry Pi OS
  does not consistently activate `graphical-session.target`, preventing a
  post-reboot kiosk outage.
- Rebuilds the locked virtual environment during setup so generated console
  launchers cannot retain a stale absolute path after a versioned-directory
  move and unpinned packages cannot survive an install.
- Preserves go-librespot's authenticated `state.json` as well as its newer
  optional `credentials.json` during a staged receiver upgrade.
- Treats both the legacy single-device `wled.host` shape and the newer device
  list as configured when selecting boot/cutover activation.
- Prevented setup from stopping the running legacy receiver or deleting the
  running system-kiosk unit before the controlled cutover.
- Added a staged installer mode that preserves boot service selection,
  graphical-session links, Wi-Fi and display power policy, and stopped
  rewriting an existing Raspotify configuration unless fallback is requested.
- Reworked the Pygame fallback so network operations and artwork loading cannot
  block or overwrite newer render state.
- Corrected LRC fractional timestamp parsing and same-track previous animation.
- Made malformed receiver/config section shapes degrade safely instead of
  producing server errors.

### Added

- Server-sent playback update notifications with polling fallback.
- Local diagnostics and adaptive browser quality controls.
- Bounded artwork/metadata/lyrics/palette caching and graceful offline state.
- Three-finger, ramped physical Waveshare backlight control with USB
  re-enumeration recovery and an 80% conservative physical cap.
- Per-device WLED reverse, phase, brightness and gamma plus atomic schema-v1
  runtime diagnostics.
- Demand-gated, single-flight WLED LAN discovery with a 512-probe ceiling and
  TTL cache instead of continuous background subnet sweeps.
- Accessible keyboard controls, modal focus management and reduced-motion
  transitions.
- Repository validation scripts, regression tests, security documentation and a
  staged Pi deployment/rollback guide.

### Changed

- Production serving uses a threaded WSGI server instead of Flask's development
  server.
- Runtime files use a dedicated `/run/spotify-display` directory.
- WLED uses path-aware dormant activation; GPIO and legacy receiver fallback are
  explicitly opt-in.
- Systemd units are rendered for the real graphical user, install path and port;
  the kiosk runs in the graphical user manager and waits for web readiness
  without requiring the Spotify receiver to be healthy.
- Dependencies are hash-locked and receiver/bootstrap downloads are checksum
  pinned to the reviewed release set; pytest tooling has a separate hashed lock
  and opt-in target installation.

### Deployment note

- Repository validation and deterministic 1080×1080 browser checks are complete.
  SSH access to the Pi is verified with its dedicated identity. Physical Pi 5
  HID/GPIO/WLED, systemd, GPU/thermal, HDMI/DPMS, network recovery and audio
  acceptance remain required during the staged live rollout.

Detailed issue mapping and verification evidence are in
[`docs/REMEDIATION.md`](docs/REMEDIATION.md).
