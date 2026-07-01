# Front-End Polish Plan — "Premium Pass"

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
3. Regression sweep (checklist below), then promote: restart `spotify-display` **and**
   `spotify-kiosk` (Jinja caches templates; Chromium caches the page).

**Local fake-playback harness:** playback-dependent changes (lyrics, accent, ring) can be
verified on a dev Mac without a receiver — in the browser console, override `window.fetch`
to serve a fake `/api/now-playing` (with a canvas-generated data-URL cover) and fake
`/api/lyrics` LRC. The whole pipeline runs: palette sampling, label render, accent
propagation, synced lyrics. Used successfully for Phases 1–2.

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

## Phase 4 — Circular-native geometry

*Make the straight-edged elements acknowledge the round canvas.*

- [ ] **Arc the crate chips.** In `renderChips()` (index.html:2174): position each chip along
      an arc concentric with the screen — per-chip
      `transform: rotate(θ) translateY(-R) rotate(-θ)` around screen center, θ spread by
      cumulative chip width. Static CSS transforms, laid out once per build. Tap targets
      unchanged (transform moves the hitbox with the chip).
- [ ] **Arc the hint text.** `#tl-hint` (index.html:895) and any bottom-rim hint → SVG
      `textPath` on an arc hugging the lower rim, same pattern as the dead-wax etching.
- [ ] **Pill capsule.** `#pill` radius 8px → ~22px (index.html:296); hairline stays
      accent-tinted from Phase 1. Nudge padding to keep the time row comfortable.
- [ ] **Rim-arc swipe feedback.** Replace the straight vertical gradient strips on
      `.skip-btn::before` (index.html:415–423, 436–443) with arc-shaped glows hugging the
      left/right rim — same visual language as the volume HUD arc. SVG or a masked radial
      gradient; driven by the existing `--swipe-strength` variable.

**Acceptance:** chips readable and tappable at both ends of the arc; swipe drag glow follows
the rim; nothing clips against the circular bezel. Screenshot set updated.

---

## Phase 5 — Quiet chrome & typographic hierarchy

*The resting state is what the device shows 95% of the time — calm it down.*

- [ ] **Auto-hide skip arrows.** Track last `pointerdown`; after ~8s without touch while
      playing, add `controls-idle` on `#viewport` → `.skip-btn` fades to 0 (currently parked
      at 0.22 forever, index.html:424). Any touch brings them back instantly. Swipe works
      regardless of arrow visibility.
- [ ] **Retire the tracklist hint.** Show `#tl-hint` for the first 3 opens
      (`localStorage` counter), then drop it.
- [ ] **Overflow titles: edge-fade + single marquee pass.** `#pill-track` (index.html:318–320):
      replace ellipsis with a right-edge `mask-image` fade; on track change, if the title
      overflows, one gentle transform-based scroll to the end and back, then settle. CSS
      animation, compositor-only.
- [ ] **Title weight.** `#pill-track`, crate `.cap-title`, `#tl-head .tl-album` → Montserrat
      600 (hosted in Phase 0). Optional experiment behind a URL param: a second display face
      for the title only — evaluate on the panel before adopting.
- [ ] **WLED modal restyle** to Phase 0 tokens (chip class, radius scale, hairline) — lowest
      priority, it's rarely seen.

**Acceptance:** after 8s untouched, the face is just record + pill + lyrics; touch anywhere
restores controls within one frame. Long titles legible without ellipsis truncation.

---

## Phase 6 — Flagship flourishes (optional, each behind a flag)

*Bigger ideas — prototype behind URL params, adopt only what earns its keep on the panel.*

- [ ] **Standby watch face** (`?standby=clock`). While dimmed, draw an ultra-dim analog clock
      reusing the tick-ring language — hands at ~6% white, accent dot at 12. One redraw per
      minute inside the existing dimmed early-return in `animate()` (index.html:1878); GPU
      stays essentially idle. (Software overlay only — panel backlight has no software control.)
- [ ] **Boot moment.** One-time ~1.2s sequence on first paint: grooves sweep in radially
      (progressive-radius `drawGrooves` over rAF), label fades and settles.
- [ ] **Crate reflection.** Single mirrored, gradient-masked copy of the *focused* card only,
      floating over the lava. One extra composited layer; kill if crate drag drops frames.
- [ ] **Tonearm progress** (`?tonearm=1`). Arm tracks lead-in → run-out with playback.
      Honest risk: clutters a clean face. Prototype, screenshot, decide.

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
- [ ] **Section switch decodes every cover.** `buildCrate()` assigns `background-image` to
      every item in the section up front, so all covers fetch + decode on a section switch
      even for cards never scrolled to. Fix: assign the image lazily in `layoutCrate` when a
      card first comes within the visible window (+2 of lookahead).
- [ ] **Pre-warm set is unbounded.** `preloadCrateImages()` warms every cover in every
      section (can be 100+ images). The pinch-in reveal only shows ~5 cards. Fix: cap the
      warm to the first ~12 per section; the lazy-assign above covers the rest on approach.
- [ ] **Grooves canvas backing store.** `#grooves` is a 1080×1080 RGBA canvas (~4.5 MB) for
      soft concentric lines. Acceptable (single static layer), but if RAM pressure shows up:
      render at 540×540 and upscale via CSS — the grooves are low-frequency and tolerate it.
- [ ] **Verify lava layers drop when hidden.** 4 blobs ≈ 842px each are composited while the
      crate is up (~11 MB GPU). They're `animation-play-state: paused` + `visibility: hidden`
      when off-screen, which *should* release the layers — confirm in `chrome://gpu` /
      DevTools layers panel on the Pi rather than assuming.
- [ ] **`tracklistCache` grows unbounded** (albumId → track arrays). Text-only, so tiny —
      cap at ~30 albums LRU only if being thorough.
- [x] **Fonts** — variable face + `unicode-range` subsets shipped in Phase 0: fewer font
      faces in RAM than the previous three Google-hosted static weights, zero network
      dependency.

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
