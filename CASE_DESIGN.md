# 3D‑Printed Enclosure — "Record Disc" Case

A parametric Fusion 360 enclosure for the **Waveshare 7" round 1080×1080 HDMI display + Raspberry Pi 5**,
styled as a mid‑century **record disc** that sits on a turntable platter (self‑centred by a spindle hole).
It includes a hidden **WLED cove** around the side wall that ties into [`wled_sync.py`](wled_sync.py).

> This file is a resume guide for the **CAD work** — the model lives in Fusion 360, not in this repo.

---

## Status (last worked 2026‑06‑14)

✅ **Complete & verified design**, sized to fit the **Bambu Lab X1 / X1C** (256×256 bed).
Final outer diameter **Ø250 mm** (~3 mm clearance per side). STEP files exported. Nothing broken;
all hardware interfaces verified.

## Files

**STEP exports** (in `~/Downloads`):
| File | Contents |
|---|---|
| `spotify_case_printed_parts_250mm.step` | **The 3 printed parts only** (Case, Cover, Diffuser) — use this for slicing |
| `spotify_record_display_case_250mm.step` | Full model incl. display + LED reference hardware |
| `spotify_case_printed_parts.step` / `spotify_record_display_case.step` | **Old Ø256 versions** (superseded) |
| `spotify_case_checkpoint_pre-led-vents.f3d` | Fusion archive checkpoint from before the lighting work |

**Fusion document:** saved in your Autodesk hub (version history). A named version checkpoint
**"pre LED channel + vents"** exists if you need to roll back the lighting/flute work.

## How to resume the CAD (Fusion 360 via MCP)

The case is driven by the **ndoo `fusion360-mcp-bridge`** (installed at `~/fusion360-mcp-bridge`,
notes in auto‑memory `fusion360-mcp.md`). To pick up:
1. Open Fusion 360, open the case document.
2. **Tools → Add‑Ins → FusionMCPBridge → Run** (look for "port 7654, token auth enabled").
3. Restart Claude Code so the `fusion360` MCP loads. Then the assistant can drive Fusion again.

---

## ⚠️ Resize gotcha (read before changing the diameter)

The model is **only partly parametric**. A one‑click `disc_dia` change does **NOT** resize cleanly.

- **Parametric (auto‑resize, safe):** the disc, cavity, back cover, glass recess, window, spindle,
  and Pi pattern — driven by `disc_dia`, `module_dia`, `window_dia`, `pi_hole_x/y`, `spindle_dia`, `wall`, etc.
- **Hardcoded (must be shifted by hand):** the **cove, twisted flutes, diffuser, screw bolt‑circle, and cable cuts**
  use absolute radii tied to the old wall.

To go from 256 → 250 we: changed `disc_dia`, **moved the screw bolt‑circle r119 → r116** (so the cover
counterbores clear the smaller cover), and **rebuilt the cove / strip / flutes / diffuser / cables 3 mm smaller**.
Any future diameter change needs the same outer‑band rebuild (a "−Δr on every outer radius" pass).

---

## The display being housed (hardware — fixed dimensions)

Waveshare "7inch 1080×1080" round HDMI LCD (datasheet drawing):
- **Cover glass Ø203.34 mm**, total module **6.45 mm** thick (1.53 mm front glass).
- **Active area Ø178.15 mm**.
- **Driver PCB 108 × 72 mm** on the rear, central; edge connectors (HDMI "Display", USB‑C "Power",
  USB "Touch", audio, buttons). Inner mounting holes are the **58 × 49 Pi pattern**.

## Printed parts (current Ø250 model)

| Part | Size | Key features |
|---|---|---|
| **CaseBody** | Ø250 × 48 mm | Record‑groove bezel; Ø203.7 flush glass recess (adhesive‑mount, 0.2 mm tol); cavity for PCB+Pi; **36 twisted (30° helix) side flutes** above the cove; WLED cove; cable exit slot + Ø6 LED pass‑through (at −Y); 6× M3 screw bosses on bolt‑circle r116 |
| **BackCover** | Ø242.4 mm | 4× **Pi 5 standoffs (58×49, M2.5)**; **Ø7.5 spindle hole** + internal guide collar; 6× M3 counterbored holes (r116); concentric base rings |
| **Diffuser** | Ø247.2 mm | **0.4 mm‑thick** translucent‑white snap ring. Top edge wedges under the cove's 45° chamfer; bottom foot snaps under an undercut lip. Flexes by material (no relief slots) |

## WLED cove

- Recessed channel ~10 mm above the base, ~8 mm deep, holds a **BTF‑Lighting WS2812B 144 LED/m** strip
  (≈107 pixels around the Ø250 ring; was 110 at Ø256). Strip is adhesive‑mounted to the cove floor.
- Strip leads drop through the **Ø6 pass‑through at −Y** into the cavity → to the Pi/controller.
- Driven by the existing [`wled_sync.py`](wled_sync.py).

## Print / assembly notes

- Parts print **separately**. At Ø250 on the 256 bed you have ~3 mm/side — use a **skirt or ≤1–2 mm brim**.
- **Diffuser:** print in **white PETG/PP** (flexes for the snap; PLA is brittle at 0.4 mm). It's delicate — handle gently.
- **Case/Cover:** PLA or PETG. The cove's top chamfer is 45° (self‑supporting); flutes/bezel print fine disc‑flat.
- **Glass:** adhesive‑mount (VHB/foam tape) into the front recess; it sits flush, retained by the bezel/rear ledge.
- Self‑tapping **M3** screws into printed bosses for the back cover; **M2.5** for the Pi.

## Verified (at Ø250)

Fits bed (Ø250) · glass nests (∩0) · PCB fits cavity (∩0) · Pi **58×49** · spindle **Ø7.5** ·
diffuser snaps (top contact + bottom lip only, no protrusion) · all 3 parts watertight single solids ·
0 stray bodies · 0 errored features.

## Possible next steps

- Export **STL/3MF per part** for the slicer (diffuser as a separate white‑filament object).
- Thermal venting if the Pi 5 runs hot (the panel has no fan and the Pi 5 runs warm — see `TROUBLESHOOTING.md`).
- Confirm the real diffuser filament and tune the 0.4 mm wall / snap engagement if too floppy.
- Optional cosmetic tweaks (flute twist amount, bezel groove density).
