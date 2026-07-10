# Multi-phase audit and remediation record

This is the implementation record for the full-project audit of Spotify
Circular Display. It records the defects found, the work completed, verification
evidence, migration decisions and remaining target-only risks. User-facing
operation stays in `README.md`; rollout and rollback stay in `DEPLOYMENT.md`.

Work was performed on `codex/multiphase-remediation`. The live Raspberry Pi was
not modified: the project is reachable on the LAN, but SSH authentication was
not available to this task. Repository completion and appliance acceptance are
therefore reported separately.

## Scope and design constraints

The project remains a single-display home appliance. The audit retained its
vinyl visual language, guest-friendly Spotify Connect path and LAN-local
operation. It did not turn Flask into an Internet-facing multi-tenant service,
move playback into a cloud dependency, or silently replace the production Pi.

The remediation used six phases so security and state contracts were fixed
before motion, hardware and operational behaviour were layered on top.

| Phase | Scope | Repository status |
|---|---|---|
| 0 | Baseline, branch, contracts and repeatable validation | Complete |
| 1 | Backend trust boundary, persistence and reliability | Complete |
| 2 | Frontend state, motion, gestures and accessibility | Complete |
| 3 | WLED, Pi 5 hardware, installer and service operation | Complete |
| 4 | Events, diagnostics, adaptive quality and new controls | Complete |
| 5 | Integration review, browser QA and release documentation | Complete in repository; Pi acceptance pending |

## Phase 0 — baseline and verification contracts

- Created the `codex/multiphase-remediation` branch and preserved unrelated
  workspace content.
- Added focused pytest coverage in `tests/test_server.py`,
  `tests/test_frontend_contract.py`, `tests/test_wled_ops.py` and
  `tests/test_backlight.py`.
- Added `scripts/validate.sh`, embedded-JavaScript parsing, deterministic unit
  rendering, a local browser fixture and CI for supported Python versions.
- Added a hash-locked Python dependency graph and pinned receiver/download
  checksums so an install no longer resolves an unreviewed dependency set.
- Added security, deployment, troubleshooting and changelog records. Project
  licensing was deliberately left unchanged for the owner to decide.

Exit result: one command now checks Python, tests, shell syntax, embedded
JavaScript, rendered service templates and whitespace integrity. Target systemd
resolution remains a Pi-side check because macOS has no systemd installation.

## Phase 1 — backend security and reliability

| ID | Audit finding | Implemented resolution |
|---|---|---|
| BE-01 | OAuth callback reflected untrusted HTML | Errors are JSON/escaped template data; auth responses are `no-store`. |
| BE-02 | OAuth lacked state, PKCE and bounded account grants | One-time state and PKCE were added; owner-approved join tokens live 10 minutes, initiate once within 5 minutes, and create expiring guest grants. Account changes invalidate tokens and caches by generation. |
| BE-03 | Private library and administration shared the public LAN boundary | Owner middleware now protects crate/library, OAuth administration, WLED mutation and detailed diagnostics. Loopback trust requires both a loopback peer and literal loopback Host, preventing Host-header rebinding. |
| BE-04 | WLED input was unbounded; malformed numbers raised 500 | Device count, strings, host/port, pixel count and renderer values have strict type/range bounds and explicit 4xx errors. |
| BE-05 | Config, refresh and recent-history writes could race or truncate | Writes are locked, permission-restricted, fsynced and atomically replaced. Token refresh is single-flight. Existing malformed/unreadable config is preserved and write-protected instead of overwritten. |
| BE-06 | Local control failure could target an unrelated Spotify device | Web API fallback is disabled by default and, when explicitly enabled, requires a configured device ID. |
| BE-07 | Album endpoints were an unrestricted metadata proxy | Public album track metadata and selection are strictly scoped to the album currently displayed; arbitrary album/track URIs are rejected. |
| BE-08 | Health could report stale legacy playback as healthy | Health reports receiver readiness, source and freshness; malformed receiver JSON becomes a bounded 503 rather than a 500. |
| BE-09 | Metadata caches and transient negative results were unbounded | Thread-safe TTL/LRU caches, retry TTLs and account-generation guards bound memory and stale publication. |
| BE-10 | Lyrics blocked duplicate workers and leaked a broad lookup surface | Current-track scoping, timeout, bounded cache, negative retry, rate limit, circuit breaker and single-flight loading were added. |
| BE-11 | WLED discovery assumed a fixed `/24`; URLs ignored custom ports and continuous sweeps wasted LAN/Pi work | Discovery derives bounded local networks, caps each scan to 512 total probes, sleeps without recent owner/idle demand, coalesces overlapping requests and preserves a TTL cache. Advertised URLs use the configured port/public origin. |
| BE-12 | Same-origin checks could trust forwarded headers or mismatched OAuth URLs | OAuth requires one explicit `public_base_url`; the redirect must use the same origin and exact `/callback` path. Cookies derive `Secure` from the public origin. Forwarded headers are not blindly trusted. |
| BE-13 | Correct JSON with wrong-shaped config sections could still crash `.get()` consumers | Typed configuration access normalizes invalid mapping/list sections and exposes a safe configuration status in health/diagnostics. |
| BE-14 | Public event streams and gesture bursts could exhaust workers or lose input | SSE client count is atomically capped; rate buckets are bounded and distinguish continuous volume/backlight gestures from strict administrative mutations. |

Key implementation: `server.py`, `templates/join.html`, `backlight.py` and the
backend test suite.

## Phase 2 — frontend motion, state and accessibility

| ID | Audit finding | Implemented resolution |
|---|---|---|
| UI-01 | Overlapping polls arrived out of order and failures looked like idle | SSE now prompts single-flight tri-state fetches; a bounded polling fallback preserves the last valid state through transport failure. |
| UI-02 | Old art/palette callbacks could overwrite a newer track | Track/art epochs make artwork, palette, labels and retry state atomic. Idle, no-art and failed-art paths commit a neutral sleeve immediately. |
| UI-03 | Same-track previous could leave the record edge-on | Manual direction/progress state separates a restart from a real previous-track flip and always completes the visual transition. |
| UI-04 | `pointercancel` committed partially completed gestures | Cancel paths restore seek, volume, platter and pointer state without sending the gesture. |
| UI-05 | Dimming measured pause time and overlays remained bright | A single user-inactivity clock controls pixels and hardware backlight; overlay stacking follows the dimmer. |
| UI-06 | Rendering continued at full cadence while dimmed/static | The high-rate RAF stops for dimmed, idle and settled paused scenes and wakes on state/activity; frame-time thresholds reduce expensive effects. |
| UI-07 | Tracklist requests and retained pointer state raced | Requests are abortable/epoch-guarded; idle closes and clears the list; all close paths clear pointers, timers and focus state. |
| UI-08 | A pinned rim-volume state was unreachable | Rim HUD state is explicit and the gesture sender is serialized/rate-compatible. |
| UI-09 | Three-digit LRC fractions were parsed as centiseconds | Fractional timestamps are normalized by digit count; offset and multiple timestamp forms are supported. |
| UI-10 | Crate art warming was repeated and unbounded | Visible/lazy warming, an LRU bound and authoritative empty-response clearing prevent memory and cross-account residue. Failures retain the last valid crate only until a successful response. |
| UI-11 | Controls/modals lacked complete keyboard and accessibility behaviour | Native buttons/roles, accessible names, focus restoration/traps, background inerting, Escape handling, tab order and reduced-motion snapping were added. |
| UI-12 | WLED UI could remain active during playback or lose concurrent edits | Playback hides/removes the setup chip from tab order and closes the modal; mutations are serialized and response epochs reject stale status. |
| UI-13 | Query-string artwork URLs were HTML-escaped in CSS declarations | CSS background URLs now use a CSS-safe serializer rather than HTML entity escaping. |

Runtime behaviour still lives in the single kiosk template to avoid a high-risk
asset-module migration during stabilization. That extraction is listed as a
future maintainability feature, not hidden unfinished remediation.

## Phase 3 — WLED and Raspberry Pi operations

| ID | Audit finding | Implemented resolution |
|---|---|---|
| OP-01 | Four-second WLED deceleration immediately dropped to paused FPS | The motor ramp retains a smooth high cadence until settled, then changes to the low paused cadence. |
| OP-02 | Idle retained stale motor state; transport errors looked idle | Playback is active/idle/error tri-state. Errors retain the last playing snapshot for an 8-second grace; real idle releases realtime mode and resets motor state. |
| OP-03 | WLED config, caches and interrupted crossfades were unsafe | Inputs/caches are bounded, palette work is reused, interrupted fades snapshot their current frame, and per-device reverse/phase/brightness/gamma are supported. |
| OP-04 | Root/non-root processes raced through `/tmp` | Runtime state moved to `/run/spotify-display` with tmpfiles ownership, locks and atomic replacement. |
| OP-05 | The watchdog restarted a healthy stack at boot | Recovery is debounced, ignores the initial healthy transition and acts only after a confirmed route/receiver failure. |
| OP-06 | Fallback was chosen by unit-file existence | Receiver health selects recovery/fallback; Raspotify remains explicit opt-in. |
| OP-07 | Units hard-coded user, path, UID, socket and fixed sleeps | Setup renders the actual user/path/port; system and graphical-user units use readiness/restart policies, target-valid path escaping and tighter service hardening. The kiosk waits for web readiness and is enabled from the Pi's active user `default.target`, so an offline receiver or inactive `graphical-session.target` cannot prevent the error UI from booting. |
| OP-08 | Pi 5 used unsupported direct `RPi.GPIO` and root crash loops | `rpi-lgpio` supplies Pi 5-compatible GPIO semantics; optional non-root buttons call the local API and are enabled only when requested. |
| OP-09 | Setup could expose credentials, install mutable downloads or tear down the live legacy stack before acceptance | Existing config is merged, mode 0600 is retained, receiver/bootstrap downloads are checksum-verified and Python uses hashed runtime/test locks. Staged mode preserves active/boot service selection, graphical links, Wi-Fi, display power policy and existing Raspotify configuration until the documented cutover. |
| OP-10 | Pygame blocked rendering, raced art and rendered continuously | Network/art work moved off the render loop, responses are epoch-checked, idle clears state and static scenes render at low rate. |

`spotify-wled.path` makes the optional renderer dormant until its configuration
exists. Runtime WLED health is atomically published as schema v1. The HID
backlight controller rediscovers USB re-enumeration, serializes/coalesces writes,
caps physical output and ramps first contact instead of exposing raw HID writes
to HTTP. The LAN discovery worker is also dormant until the owner-authorized
idle setup UI requests a scan; it never continuously sweeps during playback.

## Phase 4 — features added during remediation

- Server-sent playback change notifications with a safe polling fallback.
- Hidden on-screen diagnostics (`D` or `?diag=1`) and an owner diagnostics API.
- Frame-time adaptive quality and reduced/static motion modes.
- Three-finger physical panel brightness with idle/wake policy and radial HUD.
- Per-device WLED direction, phase, brightness and gamma controls.
- More accurate 33⅓/45 RPM classification and delta-time motor behaviour.
- Offline/error continuity instead of false track-end animation.
- Owner-approved, time-limited OAuth pairing backend and documented CLI flow.
- Hardened Chromium primary renderer plus repaired Pygame fallback.

## Phase 5 — integration and verification evidence

The release gate is `bash scripts/validate.sh && git diff --check`. It performs:

1. Python compilation.
2. The complete pytest suite.
3. Bash syntax checks for every project shell script.
4. Syntax parsing of JavaScript embedded in all templates.
5. Rendering of all systemd service/path templates.
6. Git whitespace/error checks.

Final repository results below were recorded on 2026-07-10 after the final
adversarial finding pass.

| Check | Result |
|---|---|
| Python, backend, frontend contracts, WLED/ops, Pygame fallback and backlight tests | **156 passed** |
| Python compilation, shell syntax and embedded JavaScript syntax | Passed |
| Service/path template rendering | Passed on development host |
| `git diff --check` | Passed |
| 1080×1080 playing render and live vinyl transform | Passed in deterministic in-app browser fixture |
| Rapid track/art/palette transition; keyboard pause/next | Passed in browser fixture |
| No-art and failed-art neutral fallback; idle crate transition | Passed in browser fixture |
| Transport outage continuity and recovery | Passed; last valid track remained until a valid successor |
| WLED modal focus/background state, restore and playback auto-close | Passed in browser fixture |
| Idle dim layering, hardware idle target and first-input wake drain | Passed in browser fixture |
| Browser console errors during tested scenarios | None |
| Mock 200 → 503/error → 200 recovery smoke | Passed |
| Read-only live-Pi health probe | Reachable and receiver healthy, but still serving the pre-remediation Werkzeug build and legacy `/tmp/spotify-state.json`; no mutation performed |
| Target `systemd-analyze verify` with installed dependencies | Pending on Pi |
| Physical HID, GPIO, WLED, HDMI/DPMS, audio, network recovery and thermals | Pending on Pi |

## File-by-file implementation inventory

| Area | Files | Material change |
|---|---|---|
| HTTP/API and state | `server.py` | Trust boundary, OAuth/pairing, atomic persistence, typed config/request validation, bounded caches, receiver tri-state, controls, crate/album scope, lyrics, SSE, diagnostics and demand-driven WLED discovery. |
| Browser kiosk | `templates/index.html` | Single-flight state machine, artwork/palette epochs, motion/gesture fixes, physical brightness UI, crate/tracklist/lyrics, adaptive rendering, diagnostics, WLED setup and accessibility. |
| Pairing pages | `templates/join.html` | Shared-display/expiry language and safe OAuth entry; existing `templates/connect.html` remains the post-connect/instruction page and is included in JS validation. |
| Backlight | `backlight.py` | Exact-device discovery, fixed report construction, safe logical/physical mapping, ramp/coalescing, contact identity and reconnect handling. |
| WLED renderer | `wled_sync.py`, `wled-launch.sh` | Tri-state playback tracker, smooth motor/crossfade state, per-device transforms, frame reuse, bounded config/cache and atomic runtime status; cheap disabled gate. |
| Render fallback | `display.py`, `display-launch.sh` | Nonblocking/epoch-safe Pygame networking/art, state clearing, adaptive cadence and web-readiness launch that still boots during receiver failure. |
| Physical buttons | `gpio_buttons.py` | Pi 5 `rpi-lgpio`, non-root queued/debounced actions and local API volume/playback. |
| Runtime writers/recovery | `onevent.sh`, `network_watchdog.sh`, `harden-network.sh` | Locked atomic `/run` state, debounced scoped recovery, receiver health fallback and safer unattended Wi-Fi ownership/retry policy. |
| Production server/install | `serve.sh`, `setup.sh` | Waitress, actual user/path/port rendering, verified downloads, hash-locked environment, safe config merge, exact HID udev policy and opt-in hardware/legacy modes. |
| Units/runtime directories | `services/*.service`, `services/spotify-wled.path`, `tmpfiles.d/spotify-display.conf` | Hardened system services, graphical user units, WLED activation path and shared runtime ownership. |
| Configuration/dependencies | `config.example.json`, `requirements.txt`, `requirements.lock`, `requirements-test.lock`, `pyproject.toml`, `.gitignore` | New policy keys, Pi 5 packages, separate runtime/test hashes, pytest discovery and generated/local-state exclusions. |
| Automated verification | `tests/*.py`, `scripts/validate.sh`, `scripts/check_inline_js.py`, `scripts/render_service_templates.py`, `.github/workflows/ci.yml` | Backend/security/concurrency, frontend Node contracts, WLED/ops, backlight and Pygame regressions plus compilation/syntax/template CI. |
| Browser fixture | `scripts/run_mock_display.py`, `static/mock-album.svg`, `static/mock-album-b.svg` | Deterministic playing/paused/next/no-art/failed-art/idle/error states for real 1080×1080 browser QA. |
| Operator documentation | `README.md`, `TROUBLESHOOTING.md`, `FRONTEND_POLISH_PLAN.md`, `CHANGELOG.md`, `docs/*.md` | Current architecture/use, corrected historical instructions, security boundary, phased issue record, acceptance and rollback. Licensing is intentionally unchanged. |

## Interface decisions retained

1. Spotify Connect and the small public now-playing/control surface remain
   guest-friendly on the LAN.
2. Private library data, OAuth, configuration, WLED administration and detailed
   diagnostics are owner-only.
3. Playback always distinguishes valid playback, valid idle and temporary
   transport failure.
4. SSE is an optimisation, never a single point of failure.
5. Runtime state is ephemeral under `/run/spotify-display`; durable config uses
   atomic, restrictive writes.
6. A failed local playback action never guesses another Spotify device.

## Deferred features and residual risks

These are deliberately outside the completed repository remediation:

- **Physical Pi acceptance and rollout.** SSH is now verified with the Pi's
  dedicated identity. Production remains gated on the versioned backup,
  candidate validation, controlled cutover and evidence in `DEPLOYMENT.md`.
- **Backlight interface/power validation.** The `0712:000a` composite device may
  expose more than one hidraw interface. Confirm the selected interface and HID
  report on the actual panel, observe the 10% first-contact ramp, and measure
  undervoltage/re-enumeration at the conservative 80% physical cap.
- **Graphical owner portal.** Secure pairing/status/disconnect APIs are complete,
  while link minting is intentionally documented as an owner CLI workflow. A
  QR-based local owner screen would make this friendlier.
- **Module extraction.** Split the large inline kiosk template into versioned
  JS/CSS modules once the production visual baseline has been captured.
- **Discovery refinement.** The new demand-gated subnet scanner is bounded;
  replace it with mDNS/D-Bus discovery only where Avahi ownership and socket
  coexistence are guaranteed.
- **Ambient policy.** An optional light sensor/night schedule requires a hardware
  choice and user-defined brightness policy; it was not guessed here.

None of these deferred items weakens the fixed LAN security boundary, but the
hardware items are deployment blockers for claiming live-Pi acceptance.
