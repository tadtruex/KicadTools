# SPEC: KiCad → JLCPCB Export (kicadtools)

Drives code in `src/kicadtools/`. Rules below are kept only where verification **CONFIRMED** them or a **CORRECTION** established the true behavior. Anything that stayed **uncertain/refuted** is called out as **RISK** with a safe default. Convention: "CONFIRMED" = verified against a primary source; "RISK" = undocumented/inferred, do not hard-code assumptions.

The single authoritative acceptance test for every rule here is a live round-trip: upload the generated file to JLCPCB's SMT flow (BOM tool `https://jlcpcb.com/parts/bom-tool`, and the assembly/Gerber previewer) and confirm every part maps and is oriented/sided correctly. Treat that as CI-adjacent, not the docs.

---

## 1. JLCPCB BOM CSV format

Source: https://jlcpcb.com/help/article/bill-of-materials-for-pcb-assembly , https://jlcpcb.com/help/article/how-to-generate-bom-and-centroid-files-from-kicad-8 , https://jlcpcb.com/help/article/advice-for-bom-and-cpl-files-preparation

**Columns** (CONFIRMED). Emit exactly these four headers; auto-detector maps them with no manual step:

```
Comment,Designator,Footprint,LCSC Part #
```

- `Comment` — value/spec (e.g. `100nF`, `10k`). CONFIRMED canonical. (Correction: `Value` is *also* accepted — bennymeg emits `Value` and JLC maps it. The earlier claim that `Value` steals the part-number column is **REFUTED**. Emit `Comment` anyway for zero-ambiguity mapping.)
- `Designator` — all refdes for one part **comma-joined in a SINGLE cell**, e.g. `R1,R2,R3` (CONFIRMED). Space after comma optional (JLC parses both).
- `Footprint` — package (`0402`, `SOIC-8`, …) (CONFIRMED).
- `LCSC Part #` — the LCSC C-code only (CONFIRMED as widest-used, wokwi/bennymeg emit it). JLC's own docs also label this column `JLCPCB Part #` or `LCSC PN`; all accepted. Keep `LCSC Part #`.

**Hard constraints (CONFIRMED, enforce before export):**
1. Four headers present (first three required; part-number column optional-but-recommended — blank cell = JLC asks you to pick by hand).
2. One row per unique part; all its refdes comma-joined in one cell.
3. **≤ 200 designators per row** — split larger groups across multiple rows (https://jlcpcb.com/help/article/advice-for-bom-and-cpl-files-preparation).
4. Each designator appears **exactly once** across the whole BOM (duplicates → rejection).
5. Designators must **exactly match** the CPL file (same spelling; keep case internally consistent — JLC uppercases for production but flags BOM/CPL case mismatch as "Unselected").
6. Part-number cell holds **only** LCSC `C\d+` codes, never an MPN (this was the real cause behind bennymeg #189; CORRECTION).

**Format:** UTF-8 CSV accepted (also `.xls/.xlsx`) (CONFIRMED). Comma-in-Designator cell quoted via standard CSV writer (`QUOTE_MINIMAL`) — already done in `jlcpcb.py:write_jlcpcb_csv`.

**RISKS (undocumented — safe defaults):**
- UTF-8 **BOM** marker tolerance unknown → write UTF-8 **without** BOM.
- Header token-matching algorithm / case-insensitivity undocumented → emit the exact strings above.
- "Extra columns ignored" is inferred, not documented → emit only the four columns (do not add `Quantity`; it's optional and ignored — CONFIRMED, so omit for minimality).

**Current gaps in `jlcpcb.py`:** the ≤200 split (#3) is **not** enforced — a group of >200 refs emits one oversized cell. Add splitting. Validate the LCSC cell against `^C\d+$` (#6).

---

## 2. JLCPCB CPL (centroid / pick-and-place) CSV format

Source: https://jlcpcb.com/help/article/pick-place-file-for-pcb-assembly , https://jlcpcb.com/help/article/how-to-generate-the-bom-and-centroid-file-from-kicad

**Columns** (CONFIRMED). Emit exactly:

```
Designator,Mid X,Mid Y,Layer,Rotation
```

- `Designator` — refdes, must match the BOM exactly (CONFIRMED).
- `Mid X`, `Mid Y` — component **centroid** in **mm** (CONFIRMED). Emit **bare numeric mm** (no `mm` suffix) — this is what KiCad and every community tool produce and it uploads (CONFIRMED-by-practice). The claim JLC's sample carries an `mm` suffix is **uncertain** — do not rely on suffix parsing.
- `Layer` — `Top` / `Bottom`, **capitalized** (CONFIRMED as displayed). Lowercase tolerance is undocumented → **RISK**, safe default: always normalize to `Top`/`Bottom` (KiCad's native `Side` is lowercase; convert).
- `Rotation` — degrees, **positive = counter-clockwise** (CONFIRMED). Same convention as KiCad, so no sign inversion of the base angle.

**Origin (CONFIRMED):** JLC fixes no absolute origin; the only requirement is **CPL origin == Gerber origin**. Use KiCad's **drill/place file origin (aux origin)** for both the Gerbers and the CPL. A uniform shift of all parts in the preview means the two origins disagree.

**Format:** `.csv`/`.xls`/`.xlsx`, bare mm numbers (CONFIRMED).

**Note:** JLCPCB's own repo `JLCPCB/JLCPCB-SMT-Assembly-Components-orientation-fix` is a manual walkthrough, **NOT** a per-package offset table (REFUTED) — do not source rotation offsets from it (see §3).

---

## 3. `.kicad_pcb` → CPL coordinate & rotation computation

Mirrors bennymeg Fabrication-Toolkit (`plugins/process.py`), the most-adopted tool — every formula below is CONFIRMED against its source and KiBot's notes (https://kibot.readthedocs.io/en/latest/notes_position.html). Internal KiCad units are **nanometres**; angles are CCW-positive about the footprint anchor.

Two **independent** effects cause the notorious mismatch (CONFIRMED): (a) KiCad mirrors bottom-side footprints, JLC does not; (b) KiCad vs LCSC libraries use different IPC pin-1 variants → per-family additive offsets.

### 3.1 Centroid (Mid X / Mid Y) — CONFIRMED

KiCad's `(at x y θ)` is the footprint **anchor**, not necessarily the body center. Compute the center from the **merged bounding box of all pads**:

```
pads = footprint.Pads()
if pads:
    bbox = pads[0].GetBoundingBox()
    for p in pads[1:]: bbox.Merge(p.GetBoundingBox())
    center = bbox.GetCenter()
else:
    center = footprint.GetPosition()   # fallback: no pads

aux = board.GetDesignSettings().GetAuxOrigin()
Mid_X =  (center.x - aux.x) / 1e6      # nm → mm
Mid_Y = -(center.y - aux.y) / 1e6      # nm → mm, Y inverted (KiCad Y-down → CPL Y-up)
```

- **X is NOT negated for bottom parts** (CONFIRMED). One formula, both sides — KiCad already stores each pad at its true mirrored board X. The mirror is handled entirely by the rotation rule + the global Y inversion.
- **RISK:** pad-bbox center is biased by connectors with large asymmetric mounting pads. Safe default: keep a per-footprint positional-offset override field (see 3.4).

### 3.2 Rotation — CONFIRMED pipeline

```
rot = footprint.GetOrientation().AsDegrees()   # v7+; guard: if no AsDegrees, /10.0 (tenths)
if bottom:
    rot = 180.0 - rot          # BEFORE any family/manual offset
    layer = "Bottom"
else:
    layer = "Top"
rot = (rot + family_offset + manual_offset) % 360.0
```

- Bottom rule `(180 - rot)` applied **before** offsets (CONFIRMED). **RISK:** some tools use `(360 - rot)` or `(-rot)`; these differ by 180° on asymmetric parts. Pick `(180 - rot)` (bennymeg) and **validate a known SOT-23 on the bottom** against the previewer.
- API guard (CONFIRMED): `GetOrientation().AsDegrees() if hasattr(...,'AsDegrees') else GetOrientation()/10.0`.

### 3.3 Family rotation offsets — pick ONE table, do not mix

Offsets are looked up by **regex over the footprint name**, taken as a float, positive = CCW (CONFIRMED mechanism). **The two "canonical" tables DISAGREE — mixing them yields 90–180° errors on polarized parts** (CORRECTION):

| Family (regex-ish) | JLCKicadTools `cpl_rotations_db.csv` | bennymeg `transformations.csv` |
|---|---|---|
| `^SOT-23` | **-90** | **180** |
| `^QFN-` | 270 (16/20/24/28/40) | **90** |

**CONFIRMED in BOTH (trust these):** `SOIC-/SSOP-/TSSOP-/DFN- = 270`, `LQFP-/TQFP- = 270`, `CP_EIA-/CP_Elec_ = 180`, `JST_GH_SM/JST_PH_S = 180`, `TO-277 = 90`, `LED_WS2812B-2020_PLCC4 = 90`. Special cases (JLCKicadTools): `^SOP-4_ = 0`, `^SOP-18_ = 0`, other `^SOP- = 270`; `VSSOP-8_3.0x3.0_P0.65 = 270`, other `VSSOP-8_ = 180`, `VSSOP-10_ = 270`.

**Decision:** seed from **one** source table (recommend JLCKicadTools `cpl_rotations_db.csv`, the most complete), copy its full mirror/reference convention verbatim, and **do not** cherry-pick rows from the other. Family offsets are **OPTIONAL for a first-pass file** — JLC's previewer lets you rotate parts by hand — so shipping the bottom-flip + Y-invert + aux-origin CPL with an empty offset table is already usable; the table just reduces manual corrections.

### 3.4 Positional offsets — CONFIRMED mechanism, no shipped data

Off-center-anchor parts need an x,y offset **rotated by the final rotation** (CONFIRMED formula):

```
r = rot/180*pi
top:    dx*cos(r) - dy*sin(r),  dx*sin(r) + dy*cos(r)
bottom: dx*cos(r) + dy*sin(r),  dx*sin(r) - dy*cos(r)
# add result to (Mid_X, Mid_Y)
```

**CORRECTION:** bennymeg ships **no** positional offsets — every `transformations.csv` row is `0,0`. The "USB-C X=0, Y=-1.44mm shipped default" is **fabricated** — do **not** hard-code it. Positional offsets come **only** from an explicit per-footprint override field the user sets.

### 3.5 RISK summary for §3
- SOT-23 (-90 vs 180) and QFN (270 vs 90) tables disagree → validate SOT-23 + a QFN, top and bottom, in the previewer before trusting the DB.
- Always expose a per-part **manual rotation override** and **positional offset** field.
- The "0.0635 mm per previewer arrow-press" figure is **unverified** — do not surface it as fact.

---

## 4. LCSC part-code ranking (avoiding C9900 placeholders)

Source (reverse-engineered, unofficial): `dubnubdubnub/claude-jlc-tools` SKILL.md; API `POST https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList`, JSON, no auth, results at `data.componentPageInfo.list[]` (CONFIRMED; the `/v2` variant is **unverified** — use v1). This is what `suppliers.py:jlcpcb_lcsc_for` already calls.

**CENTRAL CORRECTION — do NOT hard-drop `^C9900`:** The earlier "C9900 = non-orderable global-sourcing placeholder, exclude it" rule is **REFUTED**. The source calls `C9900…` codes **JLCPCB-internal SKUs**, and says *"pick the lower-numbered code unless the higher one has stock"* — i.e. a `C9900…` code **is orderable when it has stock**. No source links the `C9900` prefix to the Global Sourcing service, and the example `C9900016815` is **fabricated** (real example: `C9900274507`). Do not hard-code that number.

**Robust orderability gate = `stockCount > 0`** (CONFIRMED as the real signal): stock reflects JLCPCB inventory only; 0 stock ⇒ cannot be assembled regardless of C-number.

**Ranking rule (accept a match only if `_norm_mpn(componentModelEn) == _norm_mpn(query)` — already done):**
1. Hard filter: `stockCount > 0` (drop unassemblable). If none in stock → return **unresolved**, never a 0-stock code.
2. `componentLibraryType == 'base'` (Basic, no setup fee) before `'expand'` (Extended, ~$3 one-time) — CONFIRMED, and this ranks **above** stock magnitude.
3. Within `expand`, `preferredComponentFlag == true` (smaller/no setup surcharge — the "no fees" wording is overstated; treat as a tie-breaker only).
4. **Deprioritize `^C9900\d+`** (prefer the lower-numbered sibling of the same MPN) — but only as a tie-break, **not** a filter; fall back to the `C9900…` code if it alone has stock.
5. Higher `stockCount`.
6. Lowest first-tier unit price from `componentPrices[]` (documented sub-fields: `startNumber`, `productPrice`; **`endNumber` is unverified** — don't depend on it).

**Fields available** (CONFIRMED via SKILL.md): `componentCode`, `componentLibraryType` (`base`/`expand`), `stockCount`, `componentBrandEn`, `componentModelEn` (MPN), `componentPrices[]`, `preferredComponentFlag`, `leastPatchNumber`, `dataManualUrl`. Request filter `componentLibraryType`: `''`=both / `base` / `expand`.

**RISKS / safe defaults:**
- The `C9900` and the ~100M/9e9 numeric boundary rest on a **single reverse-engineered example** and no spec → never gate on the C-number regex; gate on `stockCount > 0`.
- "Empty `componentPrices` identifies a placeholder" is **uncertain** → do not use price-presence as an orderability filter.
- API is undocumented and unauthenticated → keep the existing degrade-to-`None`-on-failure behavior; log the actual `componentCode`/`stockCount`/`componentLibraryType`/prices returned for real target MPNs before trusting any heuristic.

---

## Implementation checklist (concrete code changes)

1. **`src/kicadtools/pcb.py`** — currently only reads `(attr smd|through_hole)`. Add extraction of, per refdes: `(at x y θ)` (default θ=0 when absent), side from `(layer "F.Cu"|"B.Cu")`, and pad geometry. Since the existing parser is `sexpdata`-based and has no pad bounding boxes, either (a) shell to `pcbnew` Python API for `Pads()/GetBoundingBox()/GetAuxOrigin()/GetOrientation()` (matches §3 formulas exactly), or (b) if staying pure-sexpr, compute the pad-bbox center from `(pad … (at …) (size …))` nodes and read `(setup (aux_axis_origin …))` yourself. Prefer (a) when `pcbnew` is importable; fall back to (b).

2. **New `src/kicadtools/cpl.py`** — mirror the structure of `jlcpcb.py`:
   - `CplLine` dataclass: `designator, mid_x, mid_y, layer, rotation`.
   - `build_cpl(...)` implementing §3.1–3.4: pad-bbox center, aux-origin subtract, `/1e6`, Y-invert; `rot = 180 - θ` for bottom (before offsets); `(rot + family + manual) % 360`; normalize side to `Top`/`Bottom`.
   - `CPL_FIELDS = ["Designator","Mid X","Mid Y","Layer","Rotation"]`; `write_cpl_csv(...)` with `csv.DictWriter`, UTF-8 no-BOM, bare mm numbers (format to ~4 decimals).
   - Filter to SMD-only using the existing `pcb.SMD` mounting map (reuse `build_jlcpcb_bom`'s `smd_only` pattern).

3. **New `src/kicadtools/rotations.py`** — load ONE community offset table (seed from JLCKicadTools `cpl_rotations_db.csv`), compile `regex → float degrees`, `lookup(footprint_name) -> float` (0.0 default). Add per-refdes manual rotation + positional-offset override maps (GUI-injected, like `lcsc_override` in `jlcpcb.py`). Ship with the table **empty or minimal** if uncertain — first-pass CPL is valid without it.

4. **`src/kicadtools/jlcpcb.py`** — enforce the two missing BOM invariants: split any group with `len(references) > 200` across multiple `JlcBomLine`s; validate/skip LCSC cells not matching `^C\d+$` (never emit an MPN in `LCSC Part #`). Assert no duplicate refdes across lines.

5. **`src/kicadtools/suppliers.py:jlcpcb_lcsc_for`** — replace the current "first in-stock, else first match" pick with the §4 ranking: gate on `stockCount > 0`; sort by `base` before `expand`, then `preferredComponentFlag`, then deprioritize `^C9900\d+` (tie-break only, not a filter), then higher stock, then lowest `componentPrices[0].productPrice`; return **`None`** (unresolved) when no match has stock rather than returning a 0-stock code. Do **not** add a `^C9900` hard filter.

6. **`src/kicadtools/cli.py`** — add a `cpl`/export subcommand wiring the schematic BOM + sibling `.kicad_pcb` into `build_cpl` + `write_cpl_csv`, sharing the aux origin used for Gerber export.

7. **Acceptance test (manual/E2E, not unit):** generate BOM+CPL for a board with a bottom-side SOT-23, a QFN, an electrolytic cap, and a connector; upload to JLC's BOM tool + assembly previewer; confirm all parts map, and every orientation/side/position is correct. Fix the family table per what the previewer shows before trusting it.