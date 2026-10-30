# Plan: IC part assignment

Goal: give every IC on a schematic one chosen part (MPN, manufacturer,
DigiKey PN and, when JLCPCB has it, an LCSC number), found from the part
number the symbol already carries. The footprint is checked against the
chosen part's package and flagged when it does not match or cannot be
verified; it is never changed.

This adds a third type to the Parts tab beside capacitors and resistors
(`CAPACITOR_PLAN.md`). The search is different: capacitors and resistors are
found from electrical values; an IC is found from its part number.

## Decisions (2026-10-30)

| Topic | Decision |
|---|---|
| Scope | Parts whose intent type is `ic`: `Intent:Type = ic`, or guessed as `ic` (`classify.py` already guesses it from the `U` prefix). Modules (an Arduino Nano, a radio module) are ICs for this purpose and follow the same rules; most are DNP. Transistors, diodes and LEDs are not on this path: they will be searched by value and rating, like resistors and capacitors. |
| Identifier required | A symbol needs at least one of: `MPN`, `DigiKeyPN`, `LCSC`, or an MPN-shaped token in the Value (step 2). Without one it shows "not searchable". |
| Footprint required | Yes, but as a guide. It ranks candidates and is checked against the chosen part; it never rejects a candidate and is never written. |
| Suppliers | DigiKey required. JLCPCB optional: a part JLCPCB does not stock is accepted and shown as "not at JLCPCB". |
| Stock floor | Build quantity + 1000 at DigiKey; the same at JLCPCB when the part is there. (Capacitors and resistors use the larger of 1000 and the build quantity.) |
| Manufacturer list | None. The part number fixes the manufacturer. |
| Footprint check | Footprint name parsing plus the footprint's pad count compared with DigiKey's pin count. |

## Evidence from the boards

| Board | Ref | Value | Footprint | Part numbers |
|---|---|---|---|---|
| ECG | U2 | `LMC7101BYM5` | `SOT-23-5` | none |
| ECG | U1 | `AD8232ACPZ-R7` | `ECG_Footprints:CP_20_10_ADI_NOTH` | DigiKey |
| ECG | U3 | `ABX00027` (Arduino Nano) | `ECG_Footprints:DIP30-ArduinoNano` | none |
| MotorDriver | U1, U2 | `DRV8313RHHR` | `TexasInstruments:RHH0036C` | DigiKey, LCSC |
| MotorDriver | U9 | `MotorControl_STM32L552CCT6_V2` | `LQFP-48_7x7mm_P0.5mm` | DigiKey |
| MotorDriver | U4 | `LDL212PV33R` | `STMicro:LDL212_2x2mm_Pitch0.5mm` | DigiKey `497-19548-2-ND` |
| BarrelJackAdapter | U2 | `LDL212PV33R` | `DFN-6-1EP_2x2mm_P0.65mm_EP1x1.6mm` | DigiKey `497-19548-2-ND` |
| BuckConverter | U1 | `LTC6992CS6-2` | `TSOT-23-6` | DigiKey `161-LTC6992HS6-2#…` |

- Most ICs have no `MPN` field; the MPN comes from the DigiKey PN or the
  Value.
- Several footprints are custom, so the package cannot be read from the name;
  the pad count is the check that still works.
- The two LDL212 symbols name the same DigiKey part with footprints of
  different pitch (0.5 mm and 0.65 mm). At least one is wrong.
- BuckConverter U1: the Value says C grade, the DigiKey PN says H grade.
- Not classified as `ic` today: MotorDriver U5–U8 (guessed `led`) and
  BuckConverter U3, an LTC4442 MOSFET driver (guessed `transistor`). They stay
  out of scope unless the symbol gets `Intent:Type = ic`.

## Data on the schematic

Read: `Value` and `Footprint` (KiCad's own fields, never written), the
part-number fields through `partnum.extract_part_numbers` (every alias
spelling).

Written on accept, canonical keys: `MPN`, `Manufacturer`, `DigiKeyPN`, `LCSC`
(only when JLCPCB has the part), and `Intent:Type = ic` when the type was
guessed. Empty alias spellings of the part-number fields are removed, as for
capacitors.

## Build steps

### Step 1 — `IcIntent`

`@intent_subclass("ic")` in `intent.py`: base fields only plus the part
numbers read through `extract_part_numbers`. Construction never fails for a
missing part number (that is reported by the requirement, not by the intent).

Tests: explicit and guessed `ic`; alias spellings; U parts on the fixtures.

### Step 2 — identifiers and requirement (`icrequirement.py`)

- `ic_identifiers(props) -> Identifiers`: `mpn`, `digikey_pn`, `lcsc`, and
  `value_mpns` — MPN-shaped tokens taken from the Value, longest first. The
  Value is split on spaces, `_` and `,`; a token is MPN-shaped when it has at
  least 5 characters and contains both a letter and a digit.
  `MotorControl_STM32L552CCT6_V2` gives `STM32L552CCT6` (`MotorControl` has
  no digit, `V2` is too short); `LMC7101BYM5` and `ABX00027` are kept whole;
  `1515 rgb led` gives nothing. Each token is tried as a partial MPN until
  one finds a part.
- `IcRequirement`: the identifiers, the footprint, the parsed footprint
  package (step 3), the pad count (step 4), and `problem` ("no part number",
  "no footprint").
- Grouping: by normalized identifiers + footprint, so the two LDL212 symbols
  on different boards and footprints are separate groups.

### Step 3 — footprint name parsing (`package.py`)

`parse_footprint(name) -> PackageInfo(family, pins, body_mm, pitch_mm, ep)`,
each field optional:

- family: SOT-23, TSOT-23, SOT-563, SC-70, SOIC, MSOP, TSSOP, QFN, DFN, LQFP,
  TQFP, DIP …, with KiCad's spellings (`SOT-23-5`, `QFN-24-1EP_4x4mm_P0.5mm`,
  `LQFP-48_7x7mm_P0.5mm`);
- pins from the family suffix, body from `NxMmm`, pitch from `P0.5mm` /
  `Pitch0.5mm`, exposed pad from `-1EP`.

`parse_digikey_package(params) -> PackageInfo` from `Package / Case`
(`24-VFQFN Exposed Pad`), `Supplier Device Package` (`24-QFN (4x4)`) and
`Number of Pins` when present.

Tests: every footprint and DigiKey package string in the table above.

### Step 4 — pad count from the footprint itself

Pad count = distinct non-empty pad numbers (an exposed pad counted once;
unnumbered mechanical and paste pads not counted). Sources, in order:

1. the board's `.kicad_pcb`, by reference — `pcb.parse_footprint_placements`,
   with `Pad` gaining a `number` field;
2. the footprint library: the board's `fp-lib-table` (`$KIPRJMOD` expanded),
   else `Libraries/kicad-footprints/<lib>.pretty` for KiCad's standard
   libraries; read `<name>.kicad_mod`.

No source found → pad count unknown.

Tests: fixture `mini.kicad_pcb`; a `.kicad_mod` with an exposed pad and
paste pads; `fp-lib-table` resolution with `$KIPRJMOD`.

### Step 5 — candidate search (`icsearch.py`)

`find_ic_candidates(req, *, db, digikey, jlc_search, jlc_part, needed)`, every
network call injected, like `partsearch.find_candidates`.

1. **Database:** accepted `ic` parts with the same normalized MPN.
2. **Exact lookups:** `digikey.product_for` on the DigiKey PN and the full
   MPN; `jlc_part` on the LCSC number, then `digikey.product_for` on its MPN.
3. **Partial MPN** (when the exact lookups give nothing): DigiKey keyword
   search, keeping only products whose MPN starts with the partial MPN
   (keyword results are fuzzy — see `NOTES.md`); then a JLCPCB keyword search
   on each kept MPN, exact-MPN match only.
4. **Conflicts:** identifiers that resolve to different MPNs (BuckConverter
   U1) give one candidate each, and the group shows the disagreement.
5. **Checks per candidate:** DigiKey status `Active`; DigiKey stock ≥ build
   quantity + 1000 (required); JLCPCB stock ≥ the same (when at JLCPCB);
   footprint check (step 6).
6. **Rank:** database → current part → footprint matches → at JLCPCB → JLCPCB
   Basic → DigiKey stock → price.

DigiKey calls per group: one per exact identifier, plus one keyword search
and at most `MAX_IC_LOOKUPS` (5) `product_for` calls for a partial MPN.

### Step 6 — footprint check

Compare the footprint's `PackageInfo` and pad count with the candidate's
package. Result:

- **match** — pins agree, and family, body and pitch agree wherever both
  sides state them;
- **mismatch** (red) — any stated field disagrees, or the pad count differs
  from the pin count: "Footprint does not match the part — check it very
  carefully against the datasheet." The message names what disagrees
  (`pitch 0.5 mm vs 0.65 mm`, `36 pads vs 24 pins`);
- **cannot verify** (amber) — nothing comparable on one side (custom name
  and no pad count, or DigiKey states no package): "Footprint could not be
  checked against the part — check it very carefully against the
  datasheet."

The check ranks candidates but never removes one.

### Step 7 — parts database

Schema version 2: `part.type` gains `ic`; new table
`ic (part_id, package TEXT, pins INTEGER)`. LCSC `supplier_pn` row optional
for ICs. Migration from version 1 adds the table.

### Step 8 — Cockpit

- Parts tab: IC groups as rows; Value shows the MPN or Value, Rating and
  Dielectric empty, a Footprint column with the check result's colour.
- Assign dialog for an IC: identifiers and footprint read-only; candidate
  table with MPN, manufacturer, DigiKey PN and stock, LCSC and stock (or
  "not at JLCPCB"), package, footprint check; the check message is shown
  above the Accept button for the selected candidate.
- Accept writes the part to the database and queues the write plan.

### Step 9 — live check

On scratch copies of ECG, MotorDriver, BarrelJackAdapter and BuckConverter:
every `ic` group gets a candidate or a clear reason; the LDL212 pair shows a
footprint mismatch on at least one board; BuckConverter U1 shows the C/H
grade conflict; MotorDriver U9 finds `STM32L552CCT6` from its Value; ECG U3
(Arduino Nano) is searched like any other IC.

## TBD

1. Transistors, diodes and LEDs: a separate plan on the resistor/capacitor
   path (search by value and rating), not this one.
