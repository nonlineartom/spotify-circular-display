# Changelog

All notable changes to Spotify Circular Display are documented here. The format
follows Keep a Changelog; this project does not yet publish semantic version
numbers, so audit remediation is recorded under `Unreleased`.

## Unreleased

### Changed

- Panel performance and memory pass for the 1 GB Pi:
  - All outbound HTTP (Spotify API, LRCLIB, the go-librespot loopback, WLED
    probes) now shares one process-wide pooled keep-alive session, ending the
    per-call TCP/TLS setup and socket churn from the 1s playback monitor.
  - Recent-spins persistence is batched: in-memory updates land immediately,
    the SD card sees at most one atomic write per 30 s interval plus one flush
    on shutdown or SIGTERM (a failed write stays dirty for retry).
  - Parsed configuration is cached keyed by the file's (mtime, size); atomic
    config writes always change the key, so edits are picked up without
    explicit invalidation.
  - Each SSE stream now has a hard lifetime cap (default 600 s,
    `SSE_MAX_LIFETIME_SECONDS`); EventSource auto-reconnects, so a half-dead
    socket can never pin one of the bounded stream slots forever.
  - In-memory user tokens and per-profile generations/crate caches are swept
    once their profile leaves the config or the token expires, keeping the
    dicts bounded over months-long uptimes.
  - The kiosk reconnects to the event stream with jittered exponential
    backoff (2 s → 60 s, reset on a clean open) instead of a flat 30 s.
  - The backlight controller stretches the write cadence for repeated
    identical at-rest writes (up to 1 s apart) and snaps back instantly on
    any set(), error or disconnect; ramp steps are never delayed.
  - The network watchdog backs off exponentially between consecutive
    stuck-receiver restarts (15 s → 300 s cap) so a receiver that refuses to
    re-join is not kicked every check interval; the backoff resets once a
    session is observed again.
  - systemd memory policy: go-librespot is strongly protected from the OOM
    killer (`OOMScoreAdjust=-500`), WLED is sacrificed first
    (`OOMScoreAdjust=500`), and the server (350 MB) and kiosk (600 MB) get
    `MemoryHigh` throttles so Chromium is pushed into reclaim before the
    receiver is at risk. The throttles need the host's memory cgroup
    controller: Pi firmware disables it on ≤1 GB boards, so add
    `cgroup_enable=memory` to `/boot/firmware/cmdline.txt` and reboot to arm
    them (`docs/LIVE_RELEASE_2026-08-02.md`); the OOM-score ordering works
    regardless.
  - Chromium launches with a 192 MB old-space cap, GPU rasterization, and
    background networking disabled; Waitress serves with 10 threads.
  - The boot groove sweep and crate floor reflection are now opt-in
    (`?boot=1`, `?reflect=1`) instead of default-on.

### Removed

- The animated lava-blob backdrop behind the record crate (four composited
  ~840 px blobs plus hue-bucket palette analysis) is replaced by one static
  accent-tinted radial field. Every animated `blur`/`backdrop-filter` is
  gone: the tracklist defocus is now a static opacity scrim over the frozen
  platter, which stays a cached layer at 60 fps.
- The progress ring's 60 tick marks and background track — identical every
  frame — are pre-rendered to an offscreen canvas once per accent change
  and blitted; the per-interval redraw now paints only the progress arc,
  leading dot and pause glyph. The passed-ticks glow effect went with the
  old per-frame path.
- Lyric rows are recycled from a pooled set of divs instead of an innerHTML
  teardown/rebuild per track.
- Montserrat is down to the latin subset only; cyrillic, cyrillic-ext,
  vietnamese and latin-ext faces were dropped so the kiosk never decodes or
  caches script subsets the English UI never uses.

### Changed

- The standby clock is now a large digital face: 232px mixed-weight Montserrat
  time with an accent-glow colon, a spaced date line, and the minute tick ring
  retained as a frame with the current minute lit in the album accent. Still
  one canvas redraw per minute.

- While the receiver has no session at all, the idle shelf now stays on the
  most recently authenticated household profile indefinitely (owner request)
  instead of reverting to generic House picks. Guests never persist this way,
  and an active but unlinked listener still sees House picks only.

### Fixed

- Swipe skip direction now follows carousel convention: swipe left skips to
  the next track, swipe right returns to the previous one (live feedback was
  that the restored right-for-next mapping was backwards).
- The network watchdog now probes real name resolution instead of trusting
  route presence, so router reboots that leave a dead uplink behind are
  detected; it also restarts go-librespot whenever the network is up but the
  receiver has abandoned its Spotify session (permanent reconnect give-up
  observed after the 2026-07-19 03:02–10:03 outage left casting dead).

### Security

- Added an explicit owner boundary for private library data, OAuth management,
  WLED administration and detailed diagnostics while retaining local Spotify
  Connect playback.
- Added canonical-origin, exact-callback, state-bound PKCE OAuth; one-use guest
  linkage; expiring grants; and generation-safe disconnect/cache invalidation.
- Isolated library grants by immutable Spotify account ID and bound selection,
  pairing, private fetches, cache publication and launches to the active
  receiver's opaque epoch. Unknown listeners now receive House picks only.
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
- Corrected the Waveshare touchscreen's inverted absolute axes with an
  exact-device libinput calibration rule rather than browser-coordinate hacks.
- Restored single-finger track swipes after touch calibration by preserving the
  panel's physical left/right transport mapping and ignoring normal bubbled
  pointer-capture transfers instead of treating them as cancelled contacts.
- Smoothed hardware-backlight idle and wake transitions with fine HID
  interpolation while retaining ten-point user settings, the first-contact
  ceiling, total transition time and conservative physical power limit.

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
- Receiver-aware multi-profile crates with saved albums, playlists and a
  top-listening **Your rotation** section, plus human-typeable phone pairing.
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
  The staged Pi release and reboot passed service, HID backlight, WLED,
  security, visual and thermal gates. Physical touch/audio, real playback,
  GPIO, router-outage and long-soak checks remain explicitly recorded in
  `docs/LIVE_RELEASE_2026-07-10.md`.

Detailed issue mapping and verification evidence are in
[`docs/REMEDIATION.md`](docs/REMEDIATION.md).
