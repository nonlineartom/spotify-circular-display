# Feasibility Study — Porting the Spotify Circular Display to the Waveshare ESP32‑P4‑Module

**Target board:** Waveshare *ESP32‑P4‑Module Basic Kit* (ESP32‑P4‑Module High‑Performance Dev Board, ESP32‑P4 + ESP32‑C6).
**Source system:** Raspberry Pi 5 + 7" round 1080×1080 **HDMI** panel running go‑librespot (Spotify Connect), a 1,614‑line Flask backend, a 3,523‑line Chromium‑rendered 60 fps multi‑touch web UI, WLED UDP sync, GPIO, and kiosk/networking glue.
**Date:** 2026‑06‑15 · **Method:** 10‑agent research + adversarial verification workflow (≈790k tokens, 280+ source fetches). Confidence levels and primary sources are cited throughout.

---

## 1. Verdict (TL;DR)

> **The ESP32‑P4 is a genuinely capable graphics MCU and the hardware *is viable* — but NOT as a 1:1, self‑contained replacement for the Pi box.** Two pillars of the current product do not survive the move to bare‑metal: **(a) on‑device Spotify Connect audio** and **(b) the 1080×1080 round panel at 60 fps**. Both were **independently refuted** with high confidence.

**Recommended path — "Hybrid display head" (Option A).** Keep the proven, maintained **go‑librespot + metadata server on a small always‑on Linux host** (your existing Pi 5, or a £15 Pi Zero 2 W), and make the **ESP32‑P4 a thin LVGL display + multi‑touch control head** on an **800×800 round MIPI‑DSI touch panel**. This preserves the project's single best feature — **zero‑config guest "select Pi Display" Spotify Connect with real 320 kbps audio** — plus full metadata, tracklists, lyrics, WLED, and the multi‑touch gestures, while shipping a recognizable (≈25–30 fps, RGB565) reinterpretation of the vinyl UI. It is also how essentially every successful ESP32 Spotify display in the wild is actually built (helper server + display).

A **fully self‑contained P4 box (Option B)** is *possible* but is a research project, not an integration: it requires forking and indefinitely maintaining a **dead** Spotify‑Connect library, and you lose the reliability that makes the current device "just work."

| Question you asked | Answer |
|---|---|
| Is the hardware viable? | **Yes — as a display/control head.** No — as a fully self‑contained Pi replacement. |
| Best multi‑touch screen via its own (non‑HDMI) output? | **3.4" 800×800 round MIPI‑DSI IPS + GT9271 10‑point capacitive touch** (turnkey). Adds software backlight dimming the current panel lacks. |
| Can we keep "as much functionality as possible"? | **Yes, ~85% of features** — but only by keeping audio + metadata on a Linux helper, and accepting a smaller/lower‑res screen and a degraded‑but‑faithful UI. |

---

## 2. The four decisive findings (verified)

Each was stress‑tested by a dedicated adversarial verifier doing its own searches (GitHub commit history, issue trackers, Espressif docs, vendor pages).

### 2.1 ❌ On‑device Spotify Connect audio — **REFUTED (high confidence)**
This is the project killer for a self‑contained box.

- **`cspot` (the only ESP32 Spotify‑Connect receiver) is effectively dead.** Master's last commit is **2024‑07‑12**; the `modernize` rewrite stalled **2025‑09‑30** unfinished. ([commits](https://api.github.com/repos/feelfreelinux/cspot/commits))
- **It is currently broken against 2026 Spotify.** The two newest open issues (March 2026) have **zero responses**: [#180 "Cannot fetch CDN URL → Track failed to load"](https://github.com/feelfreelinux/cspot/issues/180) and [#181 "does it still work?… nanopb build conflicts"](https://github.com/feelfreelinux/cspot/issues/181). The exact CDN breakage was **fixed in librespot** ([PR #1524](https://github.com/librespot-org/librespot/pull/1524), Aug 2025) but **never forward‑ported to cspot**. The one in‑flight cspot fix ([PR #179](https://github.com/feelfreelinux/cspot/pull/179)) is unmerged, conflicted, and *removes* the built‑in client ID — requiring per‑deployer Spotify dev credentials, which collides with the Feb 2026 lockdown (§7).
- **Chronic runtime instability**, not just protocol drift: squeezelite‑esp32 [#537 (2026‑01)](https://github.com/sle118/squeezelite-esp32/issues/537) "board reboots on Spotify connect — I2S abort! + LoadProhibited," matching a long history of crash‑on‑connect reports.
- **Zero ESP32‑P4 proof.** No cspot build, port, or success report on P4 exists. The whole stack (cspot + Tremor Vorbis decode + AES‑CTR + ES8311 I2S + ESP‑Hosted Wi‑Fi + 60 fps LVGL, all sharing 768 KB internal SRAM) coexisting on P4 is unproven.
- **No native Bluetooth fallback.** The P4 has no radio; its companion **ESP32‑C6 is BLE‑only**, and A2DP is Classic Bluetooth — so a Bluetooth‑speaker fallback is **physically impossible** on this board.
- **The protocol itself is alive** — `go-librespot` (what you run today) shipped **v0.7.3 on 2026‑05‑25** with active zeroconf work. The gap is purely cspot's lack of upkeep. **Keep go‑librespot on Linux.**

### 2.2 ❌ 1080×1080 round panel at 60 fps — **REFUTED (high confidence)**
The DSI *link* is fine; the *memory subsystem and rotation path* are not.

- **DSI bandwidth is NOT the wall.** Per Espressif's formula, 1080×1080@60 fits the 2‑lane/1.5 Gbps link in RGB565 (~872–1115 Mbps/lane). RGB888@60 (~1.3–1.7 Gbps/lane) is borderline/over.
- **PSRAM bandwidth IS the wall.** The P4's in‑package PSRAM is a **16‑bit bus @200 MHz DDR ≈ 0.8–1 GB/s**, shared by CPU + DSI readout + PPA + JPEG + audio I2S + Wi‑Fi/SDIO. Espressif's own FAQ: even improved v3.1 silicon **"stably reaches 15 fps under 1080p RGB888"** — 4× short of 60. ([ESP‑FAQ](https://docs.espressif.com/projects/esp-faq/en/latest/software-framework/peripherals/lcd.html))
- **The spinning record is a second, independent blocker.** The PPA 2D accelerator rotates **only 0/90/180/270°** — there is **no hardware path for continuous fractional‑angle rotation**. It falls back to CPU software rotation; the one rotated‑image datapoint found is **~4 fps**. ([PPA docs](https://docs.espressif.com/projects/esp-idf/en/stable/esp32p4/api-reference/peripherals/ppa.html), [LVGL #6591](https://github.com/lvgl/lvgl/issues/6591))
- **Prior art confirms the ceiling.** The near‑exact match — [Saqoosha's circular spinning‑vinyl Spotify display](https://zenn.dev/saqoosha/articles/circular-spotify-vinyl-display?locale=en) — hits **25–28 fps at 360×360** (1/9 the pixels) on ESP32‑S3 after extreme hand‑optimization, explicitly PSRAM‑bandwidth bound, and is **display‑only**.
- **No turnkey 1080 round touch panel exists for P4.** Off‑the‑shelf round P4 kits top out at **800×800 (3.4") and 720×720 (4")**. A standalone **5" 1080×1080 round (HX8399C)** panel exists but is **touch‑less** (needs a separately bonded digitizer), 127 mm square‑active (not your Ø178 mm case), and 2‑lane bring‑up on P4 is unproven.

### 2.3 ⚠️ Full UI fidelity in LVGL — **PARTIAL**
≈60% reproduces natively, ≈30% only via degradation/fakery, ≈10% drops to approximation. (Full matrix in §5.) Good news: the previously‑blocking [PPA tearing bug #9046 was **fixed** in **LVGL 9.5.0** (2026‑02‑18)](https://github.com/lvgl/lvgl/issues/9046), and **native multi‑touch gesture recognition** (pinch/rotate/two‑finger swipe + raw 10‑point access) now ships in LVGL 9.x — so the gestures are very achievable. The honest target is a **"recognizable, good‑looking, ~25–30 fps reinterpretation at 800×800,"** not a 1:1 of the Chromium UI.

### 2.4 ⚠️ Metadata / lyrics / cover‑art cloud path — **PARTIAL**
Functionally portable; **PSRAM is plentiful** (32 MB) for caches and JSON. The squeeze is **internal DMA‑capable SRAM**: TLS costs ~26–63 KB internal heap per session, Wi‑Fi/SDIO DMA descriptors and task stacks **cannot** live in PSRAM, and there is a **documented P4+C6 failure** — [esp‑hosted #597](https://github.com/espressif/esp-hosted/issues/597) "Not enough heap memory" streaming a 1280×720 JPEG over the SDIO bridge. Verdict: works **only with disciplined staging** (serialize TLS to one worker, reuse the TLS session, route cJSON/JPEG buffers to PSRAM, paginate Spotify lists 50 at a time). The Pi's *parallel* prefetch cannot be copied 1:1. **This is why a Linux helper for metadata is the low‑risk choice.**

---

## 3. Hardware reality check — the ESP32‑P4 board

| Capability | Detail | Implication for this project |
|---|---|---|
| **CPU** | Dual‑core RISC‑V HP @400 MHz + FPU + LP core | Strong for an MCU; ~one core spent on GUI, one free for net/audio. |
| **Memory** | 768 KB on‑chip L2 SRAM; **32 MB in‑package PSRAM** + 16 MB flash (DEV‑KIT = ESP32‑P4NRW32) | PSRAM bulk is generous; **768 KB internal SRAM is the true bottleneck**. |
| **PSRAM bandwidth** | 16‑bit @200 MHz DDR ≈ **0.8–1 GB/s**, shared | The hard ceiling on frame rate; forces RGB565 + cached static layers. |
| **Display out** | **MIPI‑DSI 2‑lane**, 1.5 Gbps/lane (3 Gbps). **No HDMI.** | Must use a DSI panel; your current HDMI panel cannot be reused. |
| **2D accel (PPA)** | Scale, **90°‑only rotate**, mirror, fill, blend | Helps blits/scale; **does not** help the continuous spin. |
| **JPEG codec** | HW decode ~640 px cover in **~3 ms** | Cover decode is a *strength* vs Pi/Pillow. |
| **Audio** | On‑board **ES8311 codec + NS4150B amp + 8 Ω speaker** (Basic Kit bundles speaker) | Audio *sink* is solved; the *source* (Spotify Connect) is the problem. |
| **Wi‑Fi/BT** | None native — via **ESP32‑C6** over ESP‑Hosted/SDIO; **~36 Mbps** real‑world; BLE only | Enough for a stream + metadata, but no parallel high‑BW prefetch; no BT audio. |
| **Power/thermal** | ~120–420 mA, **runs cool, no fan** | A real win over "Pi 5 runs hot, no fan" (your `display-backlight` memo). |
| **Toolchain** | ESP‑IDF 5.4/5.5 baseline (6.0 hardening). No Linux/Python/Go/browser. | Everything is rewritten in C/LVGL on FreeRTOS. |

> ⚠️ **The "Basic Kit" is the bare module + speaker.** Its DSI connector is advertised for **5/7/8/10.1" rectangular** panels; the **round** panels ship as *integrated* boards (Waveshare **ESP32‑P4‑WIFI6‑Touch‑LCD‑3.4C / ‑4C**). To get a round multi‑touch screen you should either **buy the integrated round board** or **wire a 3rd‑party round DSI panel + digitizer** to the bare module yourself (custom FPC pinout + 2.5 V DPHY rail). See §6/§10.

---

## 4. Best multi‑touch screen via the board's own output

Since you specifically want the best touch screen off the DSI output:

- **Recommended: 3.4" 800×800 round IPS DSI + GT9271 10‑point capacitive** (the Waveshare ESP32‑P4‑WIFI6‑Touch‑LCD‑3.4C panel/board). 10 points is far more than the 2 needed for twist/pinch/two‑finger gestures, contrast 1200:1, optical bonding. **Bonus:** DSI panels expose **PWM/I²C backlight brightness** — so the idle‑dimmer becomes a *real* hardware dim, fixing the limitation noted in your `display-backlight` memo and TROUBLESHOOTING.md.
- **Alternative: 4" 720×720 round** — slightly larger/physically closer to a record, a touch more FPS headroom, lower PPI.
- **If 1080 round is non‑negotiable:** standalone **5" 1080×1080 HX8399C** round panel + separately bonded GT911/GT9271 digitizer, RGB565, expect **~15–30 fps** and custom bring‑up. Not turnkey.

**Case impact:** these round panels are **~Ø115–126 mm outline** vs the current **Ø178 mm active / Ø250 mm disc** case (`CASE_DESIGN.md`, `waveshare-round-display` memo). A P4 build means a **substantially smaller case redesign** — the parametric `disc_dia`/`window_dia`/`module_dia` chain helps, but the hardcoded cove/flutes/diffuser band would need the same outer‑radius rebuild documented in CASE_DESIGN.md.

---

## 5. Feature‑by‑feature port matrix

Legend: ✅ native/easy · 🟡 doable with effort/degrade · 🟠 major rework/approximate · 🔴 drop or move off‑device.

| Feature (current) | P4 verdict | How / workaround |
|---|---|---|
| **Zero‑config guest Spotify Connect + 320 kbps audio** | 🔴 on‑device → **move to Linux helper** | cspot dead/broken (§2.1). Keep go‑librespot on Pi/Pi Zero; P4 polls its local API. |
| **Transport controls** (play/pause/next/prev/seek/volume) | ✅ (via helper) | HTTP/WebSocket calls to go‑librespot API — same calls the Flask app makes now. |
| **Spinning vinyl @33⅓/45 RPM, 60 fps** | 🟡 ~25–30 fps | Pre‑render N rotation frames per album → cycle via PPA blits; smaller platter; RGB565. No HW fractional rotate. |
| **Canvas grooves** | ✅ | Draw once to a cached `lv_canvas` layer; costs nothing during spin. |
| **Circular progress ring + gradient + dot** | ✅ | `lv_arc` + conic/linear gradient; updates ~1 Hz. |
| **Procedural curved‑text record labels** | ✅ | `lv_arclabel` (ships LVGL 9.4+) + per‑album recolor; baked once. |
| **Multi‑touch gestures** (twist scrub, volume fader, 2‑finger tap, pinch in/out) | 🟡 | LVGL native pinch/rotate/2‑finger swipe + raw 10‑pt `read_cb`; write a small gesture state machine in C. |
| **The Crate carousel** (momentum + snap) | ✅ momentum / 🟠 3D lift‑turn | `SCROLL_MOMENTUM`+`SNAP_CENTER` native; the face‑on 3D turn → fake with 2D PPA scale‑up + y‑offset + shadow (LVGL perspective is unfinished). |
| **Album tracklist view** (sink + scroll + tap‑to‑play) | 🟡 | Momentum list native; "sink + blur" → pause spin + downscale/upscale fake blur (the Pi build already pauses spin here). |
| **Lava‑lamp ambient blur blobs** | 🟠 approximate | Render 1/8‑res gradient blobs → PPA bilinear‑upscale (upscale = the diffusion), run 10–15 fps. No true Gaussian. |
| **Pinch depth blur / crossfades / z‑layers** | 🟡 | Scale is cheap (PPA); freeze spin during transition; pre‑render blurred end‑state. |
| **Album‑art decode + colour sampling** | ✅ strength | HW JPEG decode → RGB565 (~3 ms) → sample pixels in PSRAM (replaces numpy/Pillow). |
| **Synced lyrics (LRCLIB)** | ✅ | `esp_http_client` GET + LRC parse + scrolling `lv_label` with active‑line style. |
| **Track metadata / tracklists / deeper cuts / saved albums / playlists** | 🟡 on‑device · ✅ via helper | Portable (HTTPS+cJSON, paginate 50/page, PSRAM); but Feb‑2026 Spotify lockdown (§7) makes the **helper‑hosted** path safer. |
| **WLED UDP DRGB sync + discovery** | ✅ | lwIP UDP socket, same packet layout as `wled_sync.py`; mDNS `_wled._tcp` browse replaces the /24 scan. |
| **GPIO volume buttons** | ✅ | P4 has 55 GPIO; trivial. |
| **Screen dimmer (idle)** | ✅ upgrade | Real backlight PWM dim (better than the Pi software overlay). |
| **Kiosk autostart / Wi‑Fi harden / net watchdog** | ✅ different | No systemd/Chromium; firmware boots straight into the app + reconnect logic in C. |
| **Flask backend + localhost proxy** | 🔴 deleted | Replaced by FreeRTOS tasks + shared state (no "server"), or kept on the helper. |
| **1080×1080 resolution / RGB888 fidelity** | 🔴 → 800×800 RGB565 | No turnkey round 1080 touch panel; bandwidth forces RGB565. |

---

## 6. Three architecture options

### Option A — **Hybrid display head (RECOMMENDED)**
```
 Guest phone ──Spotify Connect──►  Linux helper (your Pi 5 / Pi Zero 2 W)
                                    • go-librespot  (audio out + control API)   ← UNCHANGED, maintained
                                    • slim metadata service (tracklists, art, lyrics, WLED palette)
                                          │  Wi-Fi (HTTP/WebSocket on the LAN)
                                          ▼
                                    ESP32-P4 board  ── MIPI-DSI ──►  800×800 round touch panel
                                    • LVGL vinyl UI (C/FreeRTOS)
                                    • GT9271 multi-touch gestures
                                    • drives WLED directly over UDP (optional)
```
- **Keeps:** zero‑config guest audio, 320 kbps, full metadata/tracklists/deeper cuts (server‑side ⇒ dodges the Spotify dev lockdown via your *existing* working app), lyrics, WLED, all gestures, the vinyl UI (degraded).
- **Loses:** the "single self‑contained box" ideal; needs an always‑on helper (can be a £15 Pi Zero 2 W hidden in the base, or literally the Pi 5 you already have).
- **Risk:** low. This matches the proven maker pattern and reuses your battle‑tested backend.

### Option B — **Fully self‑contained P4 box (HIGH‑RISK research project)**
Everything on the P4: a **forked, hand‑maintained cspot** (fix nanopb build, forward‑port librespot's CDN fallback, wire Tremor→ES8311, route net via C6) + on‑device metadata + LVGL UI.
- **Keeps:** the "one box" ideal, no fan.
- **Loses/risks:** reliability (crash‑on‑connect history), an **indefinite maintenance tax** every time Spotify shifts, unproven P4 coexistence of audio+GUI+TLS in 768 KB internal SRAM, and exposure to the Spotify dev lockdown if cspot needs your client credentials. **Treat as a throwaway spike first; do not couple the UI to it until proven stable on *your* board.**

### Option C — **Display‑only Web‑API poller (simplest)**
P4 polls the Spotify Web API (your phone/another device plays audio), shows art + metadata.
- **Keeps:** simplest build, no helper, no cspot.
- **Loses:** the headline feature — **guest audio on the device** — plus control‑rich flows; and runs straight into the **Feb‑2026 API limits** (Premium, 5 users/app, reduced endpoints). Good as a fallback, not the goal.

---

## 7. ⚠️ External risk — Spotify's Feb 2026 developer lockdown
Independent of hardware: Spotify ([2026‑02‑06](https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security)) tightened Web‑API Development Mode — **Premium required, 5 authorized users per app, one Client ID per developer, a reduced endpoint set**, and Extended Quota now needs a registered business (250k MAU). Implications:
- The **zeroconf/Spotify‑Connect audio path needs no developer app** → unaffected (this is why Option A's audio is safe).
- The **rich metadata path (client‑credentials token for tracklists/deeper‑cuts/art) does use the Web API** → keep that on your **existing, already‑authorized app on the helper**, not on fresh per‑device credentials. New Client IDs are now hard to get and capped.

---

## 8. Consolidated limitations · challenges · workarounds

| Limitation / challenge | Workaround |
|---|---|
| cspot dead/broken; no BT fallback | Keep go‑librespot on a Linux helper (Option A). Don't put audio on the P4. |
| ~0.8–1 GB/s PSRAM bandwidth caps FPS | RGB565; cache static layers; partial/dirty‑rect redraw; zero‑copy flush + C2M cache‑sync (esphome #16873 pattern, ~10× faster); split render/flush across cores. |
| PPA rotates 90° only (no smooth spin) | Pre‑render rotation frames per album → cycle via PPA blits; reduce platter size/frame count; or accept ~25–30 fps. |
| No turnkey round 1080 touch panel | Target 800×800 (3.4") or 720×720 (4") turnkey kits; 1080 only via custom touch‑less 5" panel + bonded digitizer. |
| 768 KB internal SRAM squeeze under TLS+Wi‑Fi+DMA | Serialize TLS to one worker; reuse session (session tickets); dynamic mbedTLS buffers; lower `SSL_IN_CONTENT_LEN`; cJSON/JPEG buffers → PSRAM; paginate 50/page. |
| ~36 Mbps Wi‑Fi via C6, internal DMA exhaustion on big JPEG over SDIO | QoS‑prioritize the audio/state socket; throttle/serialize cover prefetch; smaller cover sizes; cache aggressively. |
| No Gaussian blur / true 3D / perspective in LVGL | Box‑blur or upscale‑fake; 2D scale+shadow for "lift"; pause spin during depth transitions. |
| No systemd/kiosk/Chromium | Firmware boots into the app; reconnect/watchdog logic in C; NVS for credentials. |
| Case built around Ø178 mm panel | Redesign for ~Ø115–126 mm round panel (parametric chain + outer‑band rebuild per CASE_DESIGN.md). |

---

## 9. Proposed workflow to get it operational (Option A)

**Phase 0 — De‑risk spikes (before committing). ~1–2 weeks.**
1. Buy the **integrated 3.4" 800×800 round board** (ESP32‑P4‑WIFI6‑Touch‑LCD‑3.4C) — fastest path to real numbers. Confirm silicon rev (prefer v3.1).
2. Flash an LVGL + `esp_lvgl_port` + PPA scaffold (start from Waveshare/esp‑brookesia examples). **Measure real FPS** of: (a) a pre‑rendered‑frame spinning record at 800×800 RGB565, (b) momentum scroll, (c) a multi‑touch pinch/twist demo on GT9271.
3. Stand up the **Wi‑Fi (C6/ESP‑Hosted) + one HTTPS GET to api.spotify.com** and a JPEG cover decode; log `heap_caps_get_free_size(MALLOC_CAP_INTERNAL)` under load. Validate the memory plan.

**Phase 1 — Helper backend. ~3–5 days.**
4. On the Pi/Pi Zero: keep `go-librespot`; trim `server.py` to a **headless LAN API** (now‑playing + controls + tracklists/art/lyrics/WLED palette) exposed over HTTP/WebSocket. Reuse your existing Spotify app credentials (dodges the lockdown).

**Phase 2 — Display head firmware. ~4–8 weeks.**
5. FreeRTOS task split: **GUI task** (LVGL, one HP core) · **net/state task** (WebSocket to helper, one HP core) · **WLED UDP task** (optional).
6. Build the UI bottom‑up using §5: grooves/label/ring (cached) → spinning record (pre‑rendered frames) → crate + tracklist (momentum) → gestures → lyrics → lava‑lamp (upscale fake) → transitions (pause‑spin + scale).
7. Backlight PWM idle dimmer; reconnect/watchdog; persist config in NVS.

**Phase 3 — Integration & enclosure. ~2–4 weeks.**
8. Tune zero‑copy flush + cache‑sync + dual‑core split to hold target FPS; profile/iterate.
9. Redesign the case for the smaller round panel (CASE_DESIGN.md workflow), house the helper (Pi Zero) in the base, re‑home the WLED cove for the new diameter.

**Phase 4 — (Optional, separate track) on‑device audio spike.**
10. *Only if you want Option B later:* fork cspot on the P4, fix build + CDN fallback, validate stability for weeks. Keep it decoupled so the display still works via the helper if it breaks.

---

## 10. Bill of materials (Option A)
- **ESP32‑P4 + round touch panel** — Waveshare *ESP32‑P4‑WIFI6‑Touch‑LCD‑3.4C* (800×800 round, GT9271 10‑pt, C6 Wi‑Fi6). *(Note: the bare "Module Basic Kit" you named pairs with rectangular DSI panels; for a round screen prefer this integrated board, or plan custom panel wiring.)*
- **Audio helper** — your existing Pi 5, or a **Pi Zero 2 W** + small DAC/amp (or use the helper's own audio out) for go‑librespot.
- **(Optional)** WS2812B WLED strip/controller (unchanged from current build).
- **(Optional, 1080‑round path)** standalone 5" 1080×1080 HX8399C round DSI panel + bonded GT911/GT9271 digitizer + bare ESP32‑P4‑Module — advanced/custom only.

---

## 11. Open questions to resolve on real hardware
1. Sustained FPS of the *actual* scene (pre‑rendered spin + grooves + ring + lava‑lamp + lyrics) at 800×800 RGB565 — no equivalent benchmark exists; must prototype.
2. How many pre‑rendered rotation frames fit in PSRAM alongside cover cache + double framebuffers at a given platter size.
3. Steady‑state **internal** free heap with audio‑state WebSocket + serialized metadata TLS + LVGL + lwIP + SDIO DMA all live.
4. GT9271 report rate (need ~60–120 Hz) and whether touch registers in bezel‑masked corners.
5. Exact PSRAM/flash on your specific SKU (16 vs 32 MB) and silicon rev (v1.3 errata vs v3.1).
6. Is RGB565 acceptable for the lava‑lamp/label gradients, or is dithering needed (and its FPS cost)?

---

### Bottom line
The ESP32‑P4 is the right *class* of chip for a beautiful round touch UI and runs cool where the Pi 5 ran hot — but it **cannot reliably be the Spotify audio brain in 2026**, and it **cannot match 1080×1080@60 fps**. Build it as a **gorgeous 800×800 LVGL display + multi‑touch head backed by your existing go‑librespot host**, and you keep the magic (guest casts → music plays → vinyl spins) with low risk and ~85% of the features. Chase the fully self‑contained box only as a deliberate, decoupled research effort.
