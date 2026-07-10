# Front-End Polish Plan — "Premium Pass"

> **Historical record.** This document describes the July 2026 visual-design
> pass and is retained for its design rationale. Its old preview, service and
> rollback commands are superseded by `README.md`, `docs/REMEDIATION.md` and
> `docs/DEPLOYMENT.md`. Do not deploy or reset the current appliance from this
> file.

A phased plan to raise the perceived quality of the kiosk UI ([templates/index.html](templates/index.html)).
Each phase is independently shippable, verifiable on the preview instance before touching the
live kiosk, and ordered so later phases build on earlier ones.

## Global constraints (apply to every phase)

- **RAM is a hard constraint.** As of 2026-07 the display runs on a Pi 5 with *less* RAM than
  the original board. Chromium + Flask + go-librespot share it. Every change must be neutral
  or better on memory: minimize composited layers (`will-change` only where live), bound
  caches and pre-warm sets, avoid duplicate decoded images. Actively look for efficiency
  gains while working — log candidates in the **Efficiency backlog** below.
- **No backdrop-filter or CSS filters over moving layers.** The platter spins; anything that
  re-rasterizes per frame is out. Static composited overlays only.
- **No new per-frame JS** beyond trivial arithmetic inside the existing `animate()` loop.
- **Layout work stays off the hot path** — transforms and opacity only for anything that moves.
- Target device is the Pi 5 + Chromium kiosk at 1080×1080/60fps. Verify on the actual panel,
  not just desktop Chrome.

## Workflow per phase

1. Develop locally, commit to `main`.
2. Deploy to the Pi preview instance (`git fetch + reset`, second Flask instance on **:5001**)
   and check on the round panel itself.
3. Regression sweep (checklist below), then promote with the staged deployment
   guide. `spotify-display` is a system unit; `spotify-kiosk` is a graphical
   **user** unit.

**Current local fake-playback harness:** run
`MOCK_DISPLAY_PORT=5105 python3 scripts/run_mock_display.py`, then open
`http://127.0.0.1:5105/?diag=1`. Deterministic state endpoints are documented in
the README; browser-console fetch overrides are no longer required.

**Regression sweep (run after every phase):**
- [ ] Track skip → flip animation clean, art swaps at the hidden 90° point
- [ ] Same-album track change (no art change) still flips with A/B side toggle
- [ ] 45 Mode: single plays at 45 RPM with 7" label; ring hugs the wider label
- [ ] Pinch-in → crate; pinch-out → tracklist; both return cleanly
- [ ] Twist-seek, two-finger volume, two-finger tap
- [ ] Idle → dim → wake-tap does not trigger controls
- [ ] Lyrics sync + scroll on a track with LRCLIB lyrics
- [ ] 60fps eyeball check while spinning + while tracklist open

---

## Phase 0 — Foundations: fonts + design tokens ✅ (2026-07-01)

*Prerequisite for everything else. Low risk, mostly invisible.*

**Goal:** typography that survives a network outage; one shared vocabulary for surfaces,
motion, and radii.

- [x] **Self-host Montserrat.** Shipped as the **variable font** (wght 300–700, one file per
      script subset with `unicode-range`, ~160 KB on disk) instead of four static weights —
      Chromium only loads the subsets on-screen text uses (normally just latin, 35 KB), and
      one variable face costs less RAM than three-to-four static faces while adding the 600
      weight Phase 5 needs. `@font-face` lives in `static/fonts.css`, shared by index /
      connect / join; index.html preloads the latin file. Google Fonts links removed from all
      three templates.
- [x] **Motion tokens.** `--dur-fast/med/slow`, `--ease-glide`, `--ease-pop` in `:root`.
      Migrated: skip buttons, pill, gesture chip, WLED chip/toast, crate chips, shelf
      reveal, platter/browse curves (value-identical swap), metadata-in curve. Kept tuned:
      flip, pinch morph, lyrics (Phase 2), volume HUD 0.35s, dimmer 3s/0.45s.
- [x] **Surface + radius tokens + shared `.chip` class.** Applied to `#gesture-chip`,
      `#wled-chip`, `#wled-toast`, `#vol-label` (surfaces unified on
      `rgba(15,15,17,0.86)` + hairline). *Deviation:* `#tl-hint` left as bare text — it has
      no chip surface today and Phase 5 retires it; giving it a chip surface would have been
      a visual change out of scope for Phase 0.
- [x] **Palette plumbing (mechanism only).** `setUiAccent()` publishes `--ui-accent` +
      `--ui-accent-rgb` on `#viewport` (the `-rgb` form lets Phase 1 consumers pick their own
      alpha), guarded to value ≥ 0.72 / saturation ≤ 0.8, keyed to skip no-op style churn.
      Called from `stagePalette()`'s immediate path and the flip's hidden-90° swap, so the
      chrome tint always lands in the same frame as the artwork.

**Acceptance:** boot with Wi-Fi blocked → identical typography. No visual diffs beyond
sub-pixel timing. Regression sweep passes.

---

## Phase 1 — Palette through the chrome ✅ (2026-07-01, pending panel check)

*Highest visible impact. Small diff, mostly color plumbing.*

**Goal:** every piece of UI chrome takes its tint from the record that's playing.

- [x] **Time bar fill**: `rgba(var(--ui-accent-rgb), .75) → white`, glow accent-tinted.
- [x] **Progress ring** (canvas): ramp now interpolates guarded accent → white (leading edge
      stays a bright tip); passed ticks accent at 0.55/0.26 alpha (brighter than the old
      white — coloured light reads dimmer at equal alpha). JS reads `uiAccentArr` — no CSS
      var lookups on the draw path.
- [x] **Volume HUD**: arc fill stroked in the accent. *Deviation:* knob body stays a white
      thumb (contrast over any artwork) with an accent halo instead of a fully tinted knob.
      Leading-edge ring glow also kept white for the same reason.
- [x] **Active lyric glow**: bloom → accent at 0.30 alpha; text stays white.
- [x] **Pill hairline**: accent at 0.18 alpha.
- [x] **Bonus:** `tlAccent()` (tracklist current-row tint) now returns the *guarded* accent
      instead of the raw palette accent — consistent with the chrome and can't go muddy.
- [x] **Timing:** consumers are CSS-var/`uiAccentArr` reads; `setUiAccent` fires at the same
      moments as the lava palette incl. the flip's hidden-90° swap, so tint lands with the art.

**Acceptance (panel):** play three visually distinct albums (warm, cool, near-monochrome) —
chrome tint follows each; near-monochrome art still yields a usable accent (legibility
guard). Skip rapidly: no color flashes ahead of the art swap.

---

## Phase 2 — Lyrics engine refinement ✅ (2026-07-01, pending panel check)

*Perf + correctness fix wearing a polish hat.*

**Goal:** compositor-only lyric emphasis, stable scroll, cleaner panel.

- [x] **Scale, don't resize.** All lines lay out at one constant size (26px / weight 450 in a
      500px column — weight held constant too, since a weight change re-wraps like a size
      change). Emphasis is pure transform: active `scale(1.19)` (reads ≈ old 31px, fits the
      616px panel), inactive `scale(0.885)` (reads ≈ old 23px). Activating a line can no
      longer reflow the column, which kills both the layout thrash and the scroll drift.
- [x] **Scroll math.** Kept the `offsetTop` read (line heights legitimately vary — wrapped
      lyrics — so index×height would be wrong) but it's now *stable*, one read per line
      change, and exact. Verified: a wrapped line measures 69px next to 47px single-row
      lines with no drift.
- [x] **Spring the scroll:** `#lyrics-inner` glides on `cubic-bezier(0.3, 1.18, 0.35, 1)` —
      a touch of overshoot.
- [x] **Borderless panel:** border + white sheen gone; a single radial wash fading on all
      sides (mask still handles top/bottom). No rectangle inside the circle.
- [x] Accent glow confirmed via the fake-playback harness: teal test art → teal bloom on the
      active line over the borderless wash.

**Acceptance (panel):** long-lyric track scrolls with zero jitter; active line emphasis never
shifts neighboring lines; panel has no visible edges. *Trade to eyeball:* the lyric column is
narrower than before (500px layout) — confirm wrap frequency feels fine on real songs.

---

## Phase 3 — Vinyl light & physicality ✅ (2026-07-01, pending panel check)

*The realism pass. All static layers + trivial math in the existing per-frame transform.*

- [x] **Static specular sheen.** `#sheen` div in `#platter` between vinyl and spindle (z 7):
      conic gradient with two opposing lobes (peaks 0.085 / 0.075 at 130° / 310°), masked to
      the 152–452px donut. Does not rotate, never repaints. Fades with the furniture during
      the minimize morph and joins the `flip-hidden` treatment during the flip (the flip has
      its own sheen pulse). **Peak opacity is a panel-tuning knob** — the conic stops are the
      place to adjust after seeing it on the physical display.
- [x] **Eccentricity wobble.** `WOBBLE_PX = 0.7` orbit locked to the spin angle, scaled by
      `spinSpeed` (fades in/out with the platter), frozen under the tracklist so the blurred
      layer stays cached. Verified at runtime: translate traces the orbit, cos/sin match the
      angle, 33⅓ RPM confirmed. `WOBBLE_PX = 0` disables.
- [x] **Seating settle.** `1.004 → 1` scale across the spin-up ramp only (`SETTLE_SCALE`),
      piggybacked on the same transform string — no extra invalidations.

**Acceptance (panel):** sheen invisible as an *object*, visible as *material* — tune the
conic peaks on the physical panel. Flip, pinch morph, and tracklist blur still read
correctly. 60fps unchanged (nothing new repaints per frame).

---

## Phase 4 — Circular-native geometry ✅ (2026-07-01, pending panel check)

*Make the straight-edged elements acknowledge the round canvas.*

- [x] **Arc the crate chips.** `renderChips()` lays chips on an arc anchored 2000px below
      the row (`CHIP_ARC_R`, matching the `#crate-chips` CSS offset) — each chip tilts a few
      degrees and the outer ones sag the way the bezel does. Chips keep their tilt (no
      counter-rotation) so they read as set on the curve. Hitboxes move with the transforms.
- [x] **Arc the hint text.** `#tl-hint` is now SVG `textPath` on a 470px-radius arc hugging
      the lower rim — same construction as the dead-wax etching.
- [x] **Pill capsule.** Radius 8px → 22px; accent hairline from Phase 1 kept.
- [x] **Rim-arc swipe feedback.** The straight gradient strips are now radial gradients whose
      centre sits at the *screen* centre, so the lit band (radius ~470–540px) follows the
      bezel's curve. Still driven by `--swipe-strength`.

**Acceptance (panel):** chips readable and tappable at both ends of the arc (verify with a
full 5-section crate — dev crate only had one chip); swipe drag glow follows the rim;
nothing clips against the bezel. Screenshot set worth refreshing.

---

## Phase 5 — Quiet chrome & typographic hierarchy ✅ (2026-07-01, pending panel check)

*The resting state is what the device shows 95% of the time — calm it down.*

- [x] **Auto-hide skip arrows.** `controls-idle` on `#viewport` after 8s without touch
      (`CONTROLS_IDLE_MS`); any pointerdown wakes them. Verified live: retired at 8s,
      woke on touch. Swipes work regardless.
- [x] **Retire the tracklist hint** after 3 viewings (`localStorage.tlHintSeen`); storage
      failure keeps the hint.
- [x] **Overflow titles.** Title renders in a `.pt-inner` span; overflow adds a right
      edge-fade mask + ONE 9s transform-only marquee pass (out, hold, home), then settles
      into the fade. Verified with a deliberately long title (−1011px sweep measured).
- [x] **Title weight 600** on `#pill-track`, crate `.cap-title`, `#tl-head .tl-album` —
      served by the Phase 0 variable font at zero extra font cost. (Second-display-face
      experiment not pursued — 600 reads well; revisit only if the panel disagrees.)
- [x] **WLED modal** on tokens (hairline + panel radius).

**Acceptance (panel):** after 8s untouched, the face is just record + pill + lyrics; touch
restores controls within a frame. Long titles legible without ellipsis truncation.

---

## Phase 6 — Flagship flourishes ✅ historical trial (2026-07-01)

*Shipped as flagged prototypes; the three survivors were **defaulted ON 2026-07-02** for a
multi-day live trial. Each can be disabled via kiosk URL if the trial argues against it:
`?standby=off`, `?boot=0`, `?reflect=0`.*

- [x] **Standby watch face** — `?standby=clock`. Ultra-dim tick ring + hands + accent dot at
      12 while dimmed; one canvas redraw per minute inside the dimmed early-return. Verified
      live (drawn over the dimmed crate). Software overlay only — backlight is untouched.
- [x] **Boot moment** — `?boot=1`. Grooves sweep label→rim over ~1.2s on load
      (`drawGrooves(start, reveal)`), rim lands on the final frame.
- [x] **Crate reflection** — `?reflect=1`. One mirrored, gradient-masked slice of the focused
      sleeve; background updated only when focus changes; hides for imageless items
      (verified both paths). One extra composited layer, 374×92.
- ~~**Tonearm**~~ — prototyped (`?tonearm=1`), judged, and **removed** (2026-07-02): the
      clutter risk called out at proposal time was real. The two-circle-intersection
      approach lives in git history (`polish-v1`) if it's ever wanted again.

**Trial notes to gather on the panel:** clock brightness at night, whether the boot sweep
reads as intentional or as lag, reflection behaviour over the lava with real covers.

---

## Efficiency backlog (RAM-focused — pick up opportunistically)

**Measured baseline (2026-07-01, 1GB Pi 5, idle, 3d uptime):** 643 MB used / 346 MB
available / 468 MB swap in use. Chromium ≈ 268 MB RSS (+~160 MB swapped), live Flask
≈ 105 MB (+52 MB swap), go-librespot ≈ 19 MB. Every phase should leave these numbers
the same or better — re-measure after promoting.

Found while reading the code; each is a real win on the lower-RAM Pi. None are regressions
waiting to happen — they're bounded, mechanical changes. Tackle alongside whichever phase
touches the same code, or as a standalone efficiency pass.

- [x] **Crate card layer explosion** *(fixed with Phase 1)*. Blanket `will-change` removed
      from `.crate-card`; `layoutCrate` now promotes only cards inside the visible window
      (|d| ≤ 720px) and demotes parked ones to `auto`. Verified at runtime: 4 promoted /
      rest demoted on a 6-card section. Was ~19 MB of GPU layers on a 50-item section.
- [x] **Section switch decoded every cover.** The remediation pass now assigns/warms
      visible and near-visible covers lazily with bounded lookahead.
- [x] **Pre-warm set was unbounded.** The remediation pass caps and deduplicates warming;
      later cards load on approach.
- [ ] **Grooves canvas backing store.** `#grooves` is a 1080×1080 RGBA canvas (~4.5 MB) for
      soft concentric lines. Acceptable (single static layer), but if RAM pressure shows up:
      render at 540×540 and upscale via CSS — the grooves are low-frequency and tolerate it.
- [ ] **Verify lava layers drop when hidden.** 4 blobs ≈ 842px each are composited while the
      crate is up (~11 MB GPU). They're `animation-play-state: paused` + `visibility: hidden`
      when off-screen, which *should* release the layers — confirm in `chrome://gpu` /
      DevTools layers panel on the Pi rather than assuming.
- [x] **`tracklistCache` was unbounded.** It is now a 12-album insertion-order LRU.
- [x] **Fonts** — variable face + `unicode-range` subsets shipped in Phase 0: fewer font
      faces in RAM than the previous three Google-hosted static weights, zero network
      dependency.

## Rollback

The old tag/reset procedure has been retired because the appliance now has
versioned services, runtime paths, locked dependencies, OAuth state and optional
hardware policy that must roll back together. Use the non-destructive, backed-up
procedure in `docs/DEPLOYMENT.md`; never run `git reset --hard` against a working
appliance with unreviewed local changes.

## Suggested sequencing

| Phase | Size | Risk | Depends on |
|-------|------|------|------------|
| 0 — Foundations | M | Low | — |
| 1 — Palette chrome | S | Low | 0 |
| 2 — Lyrics engine | S–M | Medium (touches sync math) | 0 |
| 3 — Vinyl light | S–M | Medium (interacts with flip/morph) | 0 |
| 4 — Geometry | M | Medium (touches chip layout + hit targets) | 0, 1 |
| 5 — Quiet chrome | S | Low | 0, 1 |
| 6 — Flourishes | M+ | Contained (flagged) | 0–5 |

Phases 1–3 are independent of each other after Phase 0 lands — they can ship in any order,
but palette-first gives the biggest visible win soonest.
